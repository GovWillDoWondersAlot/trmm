"""
Process Spawner for HVNC.
Launches applications and browsers on the isolated Win32 Desktop with necessary workaround flags.
"""

import ctypes
from ctypes import wintypes
import os
import shutil
import time
import logging
from typing import Optional, List, Dict, Any, Tuple

logger = logging.getLogger("hvnc.spawner")

kernel32 = ctypes.windll.kernel32

# Process Creation Flags
CREATE_NEW_CONSOLE = 0x00000010
CREATE_UNICODE_ENVIRONMENT = 0x00000400
STARTF_USESHOWWINDOW = 0x00000001
SW_SHOW = 5
SW_SHOWNORMAL = 1


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD),
        ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.c_char_p),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]


kernel32.CreateProcessW.argtypes = [
    wintypes.LPCWSTR,
    wintypes.LPWSTR,
    wintypes.LPVOID,
    wintypes.LPVOID,
    wintypes.BOOL,
    wintypes.DWORD,
    wintypes.LPVOID,
    wintypes.LPCWSTR,
    ctypes.POINTER(STARTUPINFOW),
    ctypes.POINTER(PROCESS_INFORMATION),
]
kernel32.CreateProcessW.restype = wintypes.BOOL


class AppSpawner:
    """Spawns desktop applications inside a specific Win32 Desktop with compatibility flags."""

    def __init__(self, desktop_name: str = "TRMM_Hidden_Workspace"):
        self.desktop_name = desktop_name
        self.temp_root = os.path.join(os.environ.get("TEMP", "C:\\Temp"), "hvnc_workspace")
        os.makedirs(self.temp_root, exist_ok=True)

    def spawn_raw(self, app_path: Optional[str], command_line: str, work_dir: Optional[str] = None) -> Tuple[Optional[int], Optional[str]]:
        """Low-level execution of a process assigned to the hidden desktop. Returns (pid, error_str)."""
        si = STARTUPINFOW()
        si.cb = ctypes.sizeof(STARTUPINFOW)
        si.lpDesktop = self.desktop_name
        si.dwFlags = STARTF_USESHOWWINDOW
        si.wShowWindow = SW_SHOWNORMAL

        pi = PROCESS_INFORMATION()

        cmd_buffer = ctypes.create_unicode_buffer(command_line)
        flags = CREATE_NEW_CONSOLE | CREATE_UNICODE_ENVIRONMENT

        success = kernel32.CreateProcessW(
            app_path,
            cmd_buffer,
            None,
            None,
            False,
            flags,
            None,
            work_dir,
            ctypes.byref(si),
            ctypes.byref(pi),
        )

        if not success:
            err = kernel32.GetLastError()
            err_msg = ctypes.FormatError(err).strip() or f"Win32 Error {err}"
            logger.error(f"Failed to spawn '{command_line}' on desktop '{self.desktop_name}'. Error: {err_msg} ({err})")
            return None, f"Error {err}: {err_msg}"

        pid = int(pi.dwProcessId)
        kernel32.CloseHandle(pi.hThread)
        kernel32.CloseHandle(pi.hProcess)
        logger.info(f"Spawned '{command_line}' (PID: {pid}) on desktop '{self.desktop_name}'.")
        return pid, None

    def _find_binary(self, exe_name: str, candidates: List[str]) -> Optional[str]:
        """Finds binary via Windows Registry App Paths, candidate list, or PATH."""
        import winreg
        for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(root, f"Software\\Microsoft\\Windows\\CurrentVersion\\App Paths\\{exe_name}") as k:
                    val, _ = winreg.QueryValueEx(k, "")
                    if val:
                        clean_val = val.strip('"\t ')
                        if os.path.isfile(clean_val):
                            return clean_val
            except Exception:
                pass

        for path in candidates:
            expanded = os.path.expandvars(path)
            if os.path.isfile(expanded):
                return expanded

        which_path = shutil.which(exe_name)
        if which_path and os.path.isfile(which_path):
            return which_path

        return None

    def _clean_profile_locks(self, profile_dir: str):
        """Cleans up stale lock files and terminates any orphan browser processes locking profile_dir."""
        norm_prof = os.path.normpath(profile_dir).lower()
        try:
            import psutil
            for p in psutil.process_iter(['pid', 'name', 'cmdline']):
                try:
                    cmd_list = p.info.get('cmdline') or []
                    cmd_str = " ".join(cmd_list).lower()
                    if norm_prof in cmd_str:
                        logger.info(f"Terminating orphan browser process {p.info.get('pid')} ({p.info.get('name')}) locking {profile_dir}")
                        p.kill()
                except Exception:
                    pass
        except Exception as e:
            logger.debug(f"Process cleanup check error: {e}")

        time.sleep(0.1)

        if not os.path.isdir(profile_dir):
            return
        for root, dirs, files in os.walk(profile_dir):
            for fname in files:
                lower = fname.lower()
                if any(x in lower for x in ["singleton", "lock", "journal", "wal", "current session", "current tabs"]):
                    target_path = os.path.join(root, fname)
                    for _ in range(5):
                        try:
                            if os.path.exists(target_path):
                                os.remove(target_path)
                            break
                        except Exception:
                            time.sleep(0.05)

    def _prepare_cloned_profile(self, src_dir: str, dst_folder_name: str) -> str:
        """
        Clones user's browser credentials, cookies, preferences, and session data
        into an isolated persistent HVNC profile directory (Option A).
        Copies all active profiles (Default, Profile 1, Profile 2, etc.) and deep data
        including Network/Cookies, Web Data (Token Service), and Preferences.
        Excludes single-instance lock files so physical and hidden browsers run concurrently without conflict.
        """
        dst_dir = os.path.join(self.temp_root, dst_folder_name)
        os.makedirs(dst_dir, exist_ok=True)
        self._clean_profile_locks(dst_dir)

        if not os.path.isdir(src_dir):
            return dst_dir

        # 1. Copy Local State (holds master DPAPI encryption keys for passwords/cookies)
        src_local_state = os.path.join(src_dir, "Local State")
        dst_local_state = os.path.join(dst_dir, "Local State")
        if os.path.isfile(src_local_state):
            try:
                shutil.copy2(src_local_state, dst_local_state)
            except Exception as e:
                logger.debug(f"Could not copy Local State: {e}")

        # 2. Identify candidate profile directories: Default and Profile 1, Profile 2, etc.
        candidate_profiles = ["Default"]
        if os.path.isdir(src_dir):
            for entry in os.listdir(src_dir):
                if entry.startswith("Profile ") and os.path.isdir(os.path.join(src_dir, entry)):
                    candidate_profiles.append(entry)

        # Essential authentication, cookies, credentials, and settings (fast, <100ms)
        # Note: Exclude live Sessions directory to prevent Chromium SQLite journal corruption crashes
        items_to_copy = [
            "Cookies", "Login Data", "Web Data", "History", "Bookmarks",
            "Preferences", "Secure Preferences", "Network",
            "Sync Data", "Accounts"
        ]

        # Only deep copy if destination doesn't exist or is older than 10 minutes
        last_clone_marker = os.path.join(dst_dir, ".cloned_time")
        now = time.time()
        should_copy = True
        if os.path.isfile(last_clone_marker):
            try:
                with open(last_clone_marker, "r") as f:
                    t = float(f.read().strip())
                if now - t < 600.0:  # Fresh within 10 mins
                    should_copy = False
            except Exception:
                pass

        if should_copy:
            for p_folder in candidate_profiles:
                src_prof = os.path.join(src_dir, p_folder)
                dst_prof = os.path.join(dst_dir, p_folder)
                if not os.path.isdir(src_prof):
                    continue
                os.makedirs(dst_prof, exist_ok=True)

                for item in items_to_copy:
                    s = os.path.join(src_prof, item)
                    d = os.path.join(dst_prof, item)
                    if os.path.isfile(s):
                        try:
                            shutil.copy2(s, d)
                        except Exception as e:
                            logger.debug(f"Skipped file {item} in {p_folder}: {e}")
                    elif os.path.isdir(s):
                        try:
                            shutil.copytree(s, d, dirs_exist_ok=True, ignore=shutil.ignore_patterns("lockfile", "LOCK", "Singleton*", "*journal*", "*wal*", "Current Session", "Current Tabs"))
                        except Exception as e:
                            logger.debug(f"Skipped directory {item} in {p_folder}: {e}")

            try:
                with open(last_clone_marker, "w") as f:
                    f.write(str(now))
            except Exception:
                pass

        self._clean_profile_locks(dst_dir)
        return dst_dir

    def launch_chrome(self, start_url: str = "https://google.com") -> Tuple[Optional[int], Optional[str], Optional[str]]:
        """Launches Google Chrome on hidden desktop with cloned user profile & full session preservation. Returns (pid, error, binary)."""
        candidates = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
            r"%PROGRAMFILES%\Google\Chrome\Application\chrome.exe",
        ]
        binary = self._find_binary("chrome.exe", candidates)
        if not binary:
            msg = "Google Chrome binary not found on target system."
            logger.warning(msg)
            return None, msg, None

        from .cdp_controller import CDPController
        profile_dir = CDPController.get_cdp_junction_path("chrome")
        last_prof = CDPController.get_last_used_profile("chrome")

        flags = [
            '--remote-debugging-port=9222',
            '--remote-debugging-address=127.0.0.1',
            '--remote-allow-origins=*',
            f'--user-data-dir="{profile_dir}"',
            f'--profile-directory="{last_prof}"',
            "--no-profile-picker",
            "--no-sandbox",
            "--allow-no-sandbox-job",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-breakpad",
            "--disable-crash-reporter",
            "--hide-crash-restore-bubble",
            "--disable-session-crashed-bubble",
            "--start-maximized",
            f'"{start_url}"',
        ]
        cmd = f'"{binary}" ' + " ".join(flags)
        pid, err = self.spawn_raw(None, cmd)
        return pid, err, binary

    def launch_edge(self, start_url: str = "https://bing.com") -> Tuple[Optional[int], Optional[str], Optional[str]]:
        """Launches Microsoft Edge on hidden desktop with authentic user profile & full session preservation. Returns (pid, error, binary)."""
        candidates = [
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe",
            r"%PROGRAMFILES(X86)%\Microsoft\Edge\Application\msedge.exe",
        ]
        binary = self._find_binary("msedge.exe", candidates)
        if not binary:
            msg = "Microsoft Edge binary not found on target system."
            logger.warning(msg)
            return None, msg, None

        from .cdp_controller import CDPController
        profile_dir = CDPController.get_cdp_junction_path("edge")
        last_prof = CDPController.get_last_used_profile("edge")

        flags = [
            '--remote-debugging-port=9223',
            '--remote-debugging-address=127.0.0.1',
            '--remote-allow-origins=*',
            f'--user-data-dir="{profile_dir}"',
            f'--profile-directory="{last_prof}"',
            "--no-profile-picker",
            "--do-not-de-elevate",
            "--no-sandbox",
            "--test-type",
            "--allow-no-sandbox-job",
            "--disable-breakpad",
            "--disable-crash-reporter",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            "--hide-crash-restore-bubble",
            "--disable-session-crashed-bubble",
            "--disable-features=RendererCodeIntegrity,CalculateNativeWinOcclusion,msEdgeStartupBoost,msSmartScreenPua",
            "--no-service-autorun",
            "--start-maximized",
            f'"{start_url}"',
        ]
        cmd = f'"{binary}" ' + " ".join(flags)
        pid, err = self.spawn_raw(None, cmd)
        return pid, err, binary

    def launch_firefox(self, start_url: str = "https://mozilla.org") -> Tuple[Optional[int], Optional[str], Optional[str]]:
        """Launches Mozilla Firefox with an isolated profile."""
        candidates = [
            r"C:\Program Files\Mozilla Firefox\firefox.exe",
            r"C:\Program Files (x86)\Mozilla Firefox\firefox.exe",
            r"%LOCALAPPDATA%\Mozilla Firefox\firefox.exe",
        ]
        binary = self._find_binary("firefox.exe", candidates)
        if not binary:
            msg = "Mozilla Firefox binary not found on target system."
            logger.warning(msg)
            return None, msg, None

        profile_dir = os.path.join(self.temp_root, "firefox_profile")
        os.makedirs(profile_dir, exist_ok=True)
        self._clean_profile_locks(profile_dir)

        cmd = f'"{binary}" -profile "{profile_dir}" -no-remote "{start_url}"'
        pid, err = self.spawn_raw(None, cmd)
        return pid, err, binary

    def launch_opera(self, start_url: str = "https://opera.com") -> Tuple[Optional[int], Optional[str], Optional[str]]:
        """Launches Opera / Opera GX with an isolated profile."""
        candidates = [
            os.path.expandvars(r"%LOCALAPPDATA%\Programs\Opera\launcher.exe"),
            os.path.expandvars(r"%LOCALAPPDATA%\Programs\Opera GX\launcher.exe"),
            r"C:\Program Files\Opera\launcher.exe",
            r"C:\Program Files\Opera GX\launcher.exe",
        ]
        binary = self._find_binary("launcher.exe", candidates)
        if not binary:
            msg = "Opera binary not found on target system."
            logger.warning(msg)
            return None, msg, None

        profile_dir = os.path.join(self.temp_root, "opera_profile")
        os.makedirs(profile_dir, exist_ok=True)
        self._clean_profile_locks(profile_dir)

        cmd = f'"{binary}" --user-data-dir="{profile_dir}" --no-default-browser-check "{start_url}"'
        pid, err = self.spawn_raw(None, cmd)
        return pid, err, binary

    def launch_brave(self, start_url: str = "https://brave.com") -> Tuple[Optional[int], Optional[str], Optional[str]]:
        """Launches Brave Browser with an isolated profile."""
        candidates = [
            r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe"),
        ]
        binary = self._find_binary("brave.exe", candidates)
        if not binary:
            msg = "Brave Browser binary not found on target system."
            logger.warning(msg)
            return None, msg, None

        profile_dir = os.path.join(self.temp_root, "brave_profile")
        os.makedirs(profile_dir, exist_ok=True)
        self._clean_profile_locks(profile_dir)

        cmd = f'"{binary}" --user-data-dir="{profile_dir}" --no-default-browser-check "{start_url}"'
        pid, err = self.spawn_raw(None, cmd)
        return pid, err, binary

    def launch_explorer(self) -> Tuple[Optional[int], Optional[str], Optional[str]]:
        """Launches a separate File Explorer instance on the hidden desktop."""
        win_dir = os.environ.get("WINDIR", "C:\\Windows")
        explorer_path = os.path.join(win_dir, "explorer.exe")
        pid, err = self.spawn_raw(None, f'"{explorer_path}" /separate')
        return pid, err, explorer_path

    def launch_cmd(self) -> Tuple[Optional[int], Optional[str], Optional[str]]:
        """Launches Command Prompt inside Windows Console Host on hidden desktop."""
        win_dir = os.environ.get("WINDIR", "C:\\Windows")
        cmd_path = os.path.join(win_dir, "System32", "cmd.exe")
        pid, err = self.spawn_raw(None, f'conhost.exe "{cmd_path}"')
        return pid, err, cmd_path

    def launch_powershell(self) -> Tuple[Optional[int], Optional[str], Optional[str]]:
        """Launches PowerShell console inside Windows Console Host on hidden desktop."""
        win_dir = os.environ.get("WINDIR", "C:\\Windows")
        ps_path = os.path.join(win_dir, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
        pid, err = self.spawn_raw(None, f'conhost.exe "{ps_path}" -NoExit -ExecutionPolicy Bypass')
        return pid, err, ps_path

    def launch_regedit(self) -> Tuple[Optional[int], Optional[str], Optional[str]]:
        """Launches Windows Registry Editor."""
        win_dir = os.environ.get("WINDIR", "C:\\Windows")
        reg_path = os.path.join(win_dir, "regedit.exe")
        pid, err = self.spawn_raw(None, f'"{reg_path}"')
        return pid, err, reg_path

    def launch_taskmgr(self) -> Tuple[Optional[int], Optional[str], Optional[str]]:
        """Launches Task Manager."""
        win_dir = os.environ.get("WINDIR", "C:\\Windows")
        tm_path = os.path.join(win_dir, "System32", "taskmgr.exe")
        pid, err = self.spawn_raw(None, f'"{tm_path}"')
        return pid, err, tm_path

    def launch_notepad(self) -> Tuple[Optional[int], Optional[str], Optional[str]]:
        """Launches Notepad."""
        win_dir = os.environ.get("WINDIR", "C:\\Windows")
        np_path = os.path.join(win_dir, "System32", "notepad.exe")
        pid, err = self.spawn_raw(None, f'"{np_path}"')
        return pid, err, np_path

    def launch_installed_app(self, app_cmd_or_name: str, app_name: str = "") -> Tuple[Optional[int], Optional[str], Optional[str]]:
        """
        Dynamically launches any installed program on the target machine with clear diagnostics if it fails.
        """
        cmd_clean = app_cmd_or_name.strip()
        if cmd_clean.startswith('"') and cmd_clean.endswith('"') and cmd_clean.count('"') == 2:
            cmd_clean = cmd_clean[1:-1]
        lower = (app_name or app_cmd_or_name).lower()

        # Check for browser names
        if "google chrome" in lower or lower == "chrome":
            return self.launch_chrome()
        elif "chrome://settings" in lower or lower == "chrome_settings":
            return self.launch_chrome("chrome://settings")
        elif "chrome://history" in lower or lower == "chrome_history":
            return self.launch_chrome("chrome://history")
        elif "chrome://bookmarks" in lower or lower == "chrome_bookmarks":
            return self.launch_chrome("chrome://bookmarks")
        elif "chrome://downloads" in lower or lower == "chrome_downloads":
            return self.launch_chrome("chrome://downloads")
        elif "microsoft edge" in lower or lower == "edge" or lower == "msedge":
            return self.launch_edge()
        elif "mozilla firefox" in lower or "firefox" in lower:
            return self.launch_firefox()
        elif "opera gx" in lower:
            return self.launch_opera()
        elif "opera" in lower:
            return self.launch_opera()
        elif "brave" in lower:
            return self.launch_brave()
        elif "file explorer" in lower or lower == "explorer" or lower == "explorer.exe":
            return self.launch_explorer()
        elif "command prompt" in lower or lower == "cmd" or lower == "cmd.exe":
            return self.launch_cmd()
        elif "powershell" in lower:
            return self.launch_powershell()
        elif "task manager" in lower or lower == "taskmgr" or lower == "taskmgr.exe":
            return self.launch_taskmgr()
        elif "notepad" in lower:
            return self.launch_notepad()
        elif "calculator" in lower or lower in ("calc", "calc.exe"):
            # Avoid leaking UWP calc to physical screen (WinSta0\Default); launch isolated calculator in browser
            return self.launch_edge("https://www.google.com/search?q=calculator")
        elif "registry editor" in lower or lower == "regedit" or lower == "regedit.exe":
            return self.launch_regedit()
        elif cmd_clean.startswith("shell:"):
            pid, err = self.spawn_raw(None, f'explorer.exe {cmd_clean}')
            return pid, err, cmd_clean
        elif os.path.isdir(cmd_clean):
            pid, err = self.spawn_raw(None, f'explorer.exe "{cmd_clean}"')
            return pid, err, cmd_clean
        elif cmd_clean.lower().endswith(".lnk"):
            pid, err = self.spawn_raw(None, f'explorer.exe "{cmd_clean}"')
            return pid, err, cmd_clean
        elif cmd_clean.endswith(".msc"):
            # Microsoft Management Console snap-in (services.msc, eventvwr.msc, devmgmt.msc)
            win_dir = os.environ.get("WINDIR", "C:\\Windows")
            msc_path = os.path.join(win_dir, "System32", cmd_clean) if not os.path.isabs(cmd_clean) else cmd_clean
            if os.path.isfile(msc_path):
                pid, err = self.spawn_raw(None, f'mmc.exe "{msc_path}"')
                return pid, err, msc_path
            else:
                return None, f"Cannot open this app: MMC snap-in '{cmd_clean}' not found on target machine.", None

        # Generic executable resolution with argument splitting
        exe_path = None
        args_part = ""
        base_cmd = cmd_clean

        # Check if cmd_clean has arguments
        if not os.path.isfile(cmd_clean):
            import shlex
            try:
                parts = shlex.split(cmd_clean, posix=False)
                if parts and len(parts) > 1:
                    first = parts[0].strip('"')
                    if os.path.isfile(first) or shutil.which(first):
                        base_cmd = first
                        args_part = " " + " ".join(parts[1:])
            except Exception:
                pass

        if os.path.isfile(base_cmd):
            if base_cmd.lower().endswith((".exe", ".bat", ".cmd")):
                exe_path = base_cmd
            else:
                pid, err = self.spawn_raw(None, f'explorer.exe "{base_cmd}"')
                return pid, err, base_cmd
        else:
            which = shutil.which(base_cmd)
            if which and os.path.isfile(which):
                exe_path = which

        if not exe_path:
            return None, f"Cannot open this app: Executable '{base_cmd}' not found on the target system.", None

        if exe_path.lower().endswith((".ico", ".png", ".jpg")):
            return None, f"Cannot open this app: '{os.path.basename(exe_path)}' is not an executable file.", None

        full_spawn_cmd = f'"{exe_path}"{args_part}'
        pid, err = self.spawn_raw(None, full_spawn_cmd)
        if err:
            return None, f"Cannot open this app: {err}", exe_path
        return pid, None, exe_path

    def get_installed_apps(self) -> List[Dict[str, Any]]:
        """
        Dynamically discovers all programs and browsers installed on the target machine.
        Returns a sorted, categorized, deduplicated list.
        """
        import winreg
        apps = []
        seen = set()

        # 1. Check common browsers first
        browsers = [
            ("Google Chrome", [
                r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe")
            ], "🌐"),
            ("Microsoft Edge", [
                r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"
            ], "🌐"),
            ("Mozilla Firefox", [
                r"C:\Program Files\Mozilla Firefox\firefox.exe",
                r"C:\Program Files (x86)\Mozilla Firefox\firefox.exe"
            ], "🦊"),
            ("Opera", [
                os.path.expandvars(r"%LOCALAPPDATA%\Programs\Opera\launcher.exe"),
                r"C:\Program Files\Opera\launcher.exe"
            ], "🔴"),
            ("Opera GX", [
                os.path.expandvars(r"%LOCALAPPDATA%\Programs\Opera GX\launcher.exe")
            ], "🎮"),
            ("Brave Browser", [
                r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
                os.path.expandvars(r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe")
            ], "🦁")
        ]
        for b_name, paths, icon in browsers:
            for p in paths:
                if os.path.exists(p):
                    apps.append({"name": b_name, "cmd": p, "category": "Web Browsers", "icon": icon})
                    seen.add(b_name.lower())
                    break

        # 2. Built-in Windows utilities
        sys_tools = [
            ("File Explorer", "explorer.exe", "System Utilities", "📁"),
            ("Command Prompt", "cmd.exe", "System Utilities", "⌨️"),
            ("Windows PowerShell", "powershell.exe", "System Utilities", "💻"),
            ("Task Manager", "taskmgr.exe", "System Utilities", "📊"),
            ("Notepad", "notepad.exe", "System Utilities", "📝"),
            ("Calculator", "calculator", "System Utilities", "🧮"),
            ("Registry Editor", "regedit.exe", "System Utilities", "⚙️"),
            ("Windows Services", "services.msc", "System Utilities", "🔧"),
            ("Event Viewer", "eventvwr.msc", "System Utilities", "📜")
        ]
        for t_name, t_cmd, cat, icon in sys_tools:
            apps.append({"name": t_name, "cmd": t_cmd, "category": cat, "icon": icon})
            seen.add(t_name.lower())

        # 3. Registry installed programs (HKLM, HKLM WOW64, HKCU)
        reg_paths = [
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
            (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        ]

        for root, subkey in reg_paths:
            try:
                with winreg.OpenKey(root, subkey) as key:
                    num_subkeys = winreg.QueryInfoKey(key)[0]
                    for i in range(num_subkeys):
                        try:
                            k_name = winreg.EnumKey(key, i)
                            with winreg.OpenKey(key, k_name) as app_key:
                                try:
                                    name, _ = winreg.QueryValueEx(app_key, "DisplayName")
                                    if not name or name.lower() in seen:
                                        continue

                                    sys_comp = 0
                                    try:
                                        sys_comp, _ = winreg.QueryValueEx(app_key, "SystemComponent")
                                    except Exception:
                                        pass
                                    if sys_comp:
                                        continue

                                    cmd = ""
                                    try:
                                        cmd, _ = winreg.QueryValueEx(app_key, "DisplayIcon")
                                        cmd = cmd.split(",")[0].strip('"')
                                    except Exception:
                                        pass

                                    if not cmd:
                                        try:
                                            cmd, _ = winreg.QueryValueEx(app_key, "InstallLocation")
                                        except Exception:
                                            pass

                                    seen.add(name.lower())
                                    apps.append({
                                        "name": name,
                                        "cmd": cmd or name,
                                        "category": "Installed Programs",
                                        "icon": "📦"
                                    })
                                except Exception:
                                    pass
                        except Exception:
                            pass
            except Exception:
                pass

        return sorted(apps, key=lambda x: (0 if x["category"] == "Web Browsers" else 1, x["name"].lower()))
