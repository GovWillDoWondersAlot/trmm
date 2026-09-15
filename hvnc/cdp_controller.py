"""
Tactical RMM — Chrome DevTools Protocol (CDP) Controller Bridge.
Manages connection to local Chromium instances running with --remote-debugging-port,
queries open tabs, subscribes to screencast frames, and dispatches browser input.
"""

import os
import time
import subprocess
import json
import logging
import urllib.request
import urllib.error
import asyncio
from typing import Optional, Dict, Any, List, Callable

logger = logging.getLogger("hvnc.cdp_controller")

DEFAULT_CHROME_PORT = 9222
DEFAULT_EDGE_PORT = 9223


class CDPController:
    """
    Interfaces with Chromium browsers via Chrome DevTools Protocol over local HTTP/WebSocket.
    Provides tab discovery, live screencast frame reception, and input dispatching.
    """

    def __init__(self, port: int = DEFAULT_CHROME_PORT, host: str = "127.0.0.1"):
        self.port = port
        self.host = host
        self.base_url = f"http://{host}:{port}"
        self.active_tab_id: Optional[str] = None
        self.ws_url: Optional[str] = None
        self.ws = None
        self.is_streaming = False
        self._command_id = 0
        self._response_futures: Dict[int, asyncio.Future] = {}
        self.frame_callback: Optional[Callable[[bytes], None]] = None

    def is_port_open(self) -> bool:
        """Checks if the local debugging endpoint is listening."""
        try:
            req = urllib.request.Request(f"{self.base_url}/json/version", headers={"User-Agent": "TRMM-CDP-Client"})
            with urllib.request.urlopen(req, timeout=1.5) as resp:
                return resp.status == 200
        except Exception:
            return False

    def list_tabs(self) -> List[Dict[str, Any]]:
        """
        Retrieves all open inspectable pages/tabs from the browser.
        Returns list of tab metadata (id, title, url, webSocketDebuggerUrl).
        """
        try:
            req = urllib.request.Request(f"{self.base_url}/json/list", headers={"User-Agent": "TRMM-CDP-Client"})
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                # Filter for page targets (actual tabs, not background service workers or omnibox popups)
                pages = [t for t in data if t.get("type") == "page" and not t.get("url", "").startswith("chrome://omnibox")]
                return pages if pages else [t for t in data if t.get("type") == "page"]
        except Exception as e:
            logger.debug(f"CDP list_tabs error on {self.base_url}: {e}")
            return []

    @staticmethod
    def find_browser_binary(browser: str = "chrome") -> Optional[str]:
        """Locates the installed executable for Google Chrome or Microsoft Edge."""
        import os
        if browser == "edge":
            candidates = [
                r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
                os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe"),
            ]
        else:
            candidates = [
                r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
                os.path.expandvars(r"%PROGRAMFILES%\Google\Chrome\Application\chrome.exe"),
            ]
        for p in candidates:
            if os.path.isfile(p):
                return p
        return None

    @staticmethod
    def get_user_data_path(browser: str = "chrome") -> str:
        """Returns the canonical User Data path for Chrome or Edge."""
        import os
        if browser == "edge":
            return os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\User Data")
        return os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\User Data")

    @staticmethod
    def get_cdp_junction_path(browser: str = "chrome") -> str:
        """
        Returns a cloned isolated profile directory for CDP debugging sessions.
        Deep-clones the user's authentic profile data (passwords, logins, bookmarks,
        history, preferences, accounts, extensions) into an isolated directory
        while excluding Chromium's process singleton locks (SingletonLock, Lockfile).
        This allows Chrome/Edge on Backstage to run with 100% of the authentic user profile
        concurrently alongside the user's active browser without collision.
        """
        import shutil
        if browser == "edge":
            user_data = os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\User Data")
            cdp_data = os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\TRMM_CDP_Isolated")
        else:
            user_data = os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\User Data")
            cdp_data = os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\TRMM_CDP_Isolated")

        os.makedirs(cdp_data, exist_ok=True)
        if not os.path.isdir(user_data):
            return cdp_data

        # 1. Clean any stale singleton locks in cdp_data
        for root, dirs, files in os.walk(cdp_data):
            for fname in files:
                lower = fname.lower()
                if any(x in lower for x in ["singleton", "lock", "journal", "wal", "current session", "current tabs"]):
                    try:
                        os.remove(os.path.join(root, fname))
                    except Exception:
                        pass

        # 2. Check clone freshness: only deep clone if clone is missing or older than 5 minutes
        last_clone_marker = os.path.join(cdp_data, ".cloned_time")
        now = time.time()
        should_clone = True
        if os.path.isfile(last_clone_marker):
            try:
                with open(last_clone_marker, "r") as f:
                    t = float(f.read().strip())
                if now - t < 300.0:  # Fresh within 5 minutes
                    should_clone = False
            except Exception:
                pass

        if should_clone:
            # Copy Local State (holds DPAPI master encryption keys for passwords/cookies)
            src_state = os.path.join(user_data, "Local State")
            dst_state = os.path.join(cdp_data, "Local State")
            if os.path.isfile(src_state):
                try:
                    shutil.copy2(src_state, dst_state)
                except Exception as e:
                    logger.debug(f"Could not copy Local State: {e}")

            # Identify candidate profiles: Default and Profile 1, Profile 2, etc.
            candidate_profiles = ["Default"]
            for entry in os.listdir(user_data):
                if entry.startswith("Profile ") and os.path.isdir(os.path.join(user_data, entry)):
                    candidate_profiles.append(entry)

            items_to_copy = [
                "Preferences", "Secure Preferences", "Bookmarks", "Bookmarks.bak",
                "History", "Login Data", "Login Data For Account", "Web Data",
                "Shortcuts", "Top Sites", "Favicons", "Affiliation Database",
                "Account Web Data", "Extension Cookies", "Sync Data", "Accounts"
            ]

            for p_folder in candidate_profiles:
                src_prof = os.path.join(user_data, p_folder)
                dst_prof = os.path.join(cdp_data, p_folder)
                if not os.path.isdir(src_prof):
                    continue
                os.makedirs(dst_prof, exist_ok=True)

                for item in items_to_copy:
                    s_item = os.path.join(src_prof, item)
                    d_item = os.path.join(dst_prof, item)
                    if os.path.isfile(s_item):
                        try:
                            shutil.copy2(s_item, d_item)
                        except Exception:
                            pass

                # Resilient file-by-file copy for Network folder (cookies, network state)
                src_net = os.path.join(src_prof, "Network")
                dst_net = os.path.join(dst_prof, "Network")
                if os.path.isdir(src_net):
                    os.makedirs(dst_net, exist_ok=True)
                    for nf in os.listdir(src_net):
                        snf = os.path.join(src_net, nf)
                        dnf = os.path.join(dst_net, nf)
                        if os.path.isfile(snf):
                            try:
                                shutil.copy2(snf, dnf)
                            except Exception:
                                pass

                # Copy Extensions folder so all user extensions are preserved
                src_ext = os.path.join(src_prof, "Extensions")
                dst_ext = os.path.join(dst_prof, "Extensions")
                if os.path.isdir(src_ext):
                    try:
                        shutil.copytree(src_ext, dst_ext, dirs_exist_ok=True, ignore=shutil.ignore_patterns("*.tmp", "*lock*"))
                    except Exception:
                        pass

            try:
                with open(last_clone_marker, "w") as f:
                    f.write(str(now))
            except Exception:
                pass

        # Final lock cleanup in destination
        for root, dirs, files in os.walk(cdp_data):
            for fname in files:
                lower = fname.lower()
                if any(x in lower for x in ["singleton", "lock", "journal", "wal", "current session", "current tabs"]):
                    try:
                        os.remove(os.path.join(root, fname))
                    except Exception:
                        pass

        return cdp_data

    @staticmethod
    def get_last_used_profile(browser: str = "chrome") -> str:
        """Reads Local State to detect the user's active/last-used profile to prevent profile picker dialogs."""
        import os
        user_data = CDPController.get_user_data_path(browser)
        local_state_file = os.path.join(user_data, "Local State")
        if os.path.exists(local_state_file):
            try:
                with open(local_state_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    return data.get("profile", {}).get("last_used", "Default")
            except Exception:
                pass
        return "Default"

    @staticmethod
    def configure_persistent_cdp(browser: str = "chrome", port: int = DEFAULT_CHROME_PORT) -> bool:
        """
        Configures the user's auto-launch and shortcuts to automatically include
        CDP flags, so future browser launches have the port open seamlessly.
        """
        import os
        import winreg
        try:
            cdp_data = CDPController.get_cdp_junction_path(browser)
            binary = CDPController.find_browser_binary(browser)
            if not binary:
                return False

            # Update HKCU Run key if Chrome autolaunch is registered
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_READ | winreg.KEY_WRITE) as k:
                i = 0
                while True:
                    try:
                        vn, vv, _ = winreg.EnumValue(k, i)
                        if browser in vn.lower() or browser in vv.lower():
                            new_cmd = f'"{binary}" --remote-debugging-port={port} --remote-debugging-address=127.0.0.1 --remote-allow-origins=* --user-data-dir="{cdp_data}" --no-startup-window'
                            winreg.SetValueEx(k, vn, 0, winreg.REG_SZ, new_cmd)
                            logger.info(f"Updated HKCU Run entry '{vn}' with CDP flags.")
                        i += 1
                    except OSError:
                        break
            return True
        except Exception as e:
            logger.debug(f"configure_persistent_cdp exception: {e}")
            return False

    def ensure_browser_listening(self, browser: str = "chrome", timeout: float = 12.0, desktop_name: Optional[str] = None) -> bool:
        """
        Checks if the debugging port is listening. If not:
        1. Checks if the browser is running without debugging port; if so, terminates it to free profile locks.
           (When running in Backstage with desktop_name, only cleans up isolated junction processes, never user's real browser).
        2. Sets up a discreet junction user data directory so Chromium enables CDP without profile picker dialogs.
        3. Relaunches with session restoration on the isolated desktop (or detached) and waits until debugging API responds.
        """
        import time
        import subprocess

        if self.is_port_open():
            return True

        binary = self.find_browser_binary(browser)
        if not binary:
            logger.warning(f"Cannot auto-launch: {browser} binary not found.")
            return False

        cdp_data = self.get_cdp_junction_path(browser)
        last_profile = self.get_last_used_profile(browser)

        # Free profile locks. In Backstage mode, only terminate processes using our junction path
        # to guarantee zero disturbance to the physical user's running browser session!
        try:
            import psutil
            browser_exe = "msedge.exe" if browser == "edge" else "chrome.exe"
            norm_cdp_data = os.path.normpath(cdp_data).lower()
            running_procs = []
            for p in psutil.process_iter(['name', 'pid', 'cmdline']):
                try:
                    if p.info['name'] and p.info['name'].lower() == browser_exe:
                        cmd_str = " ".join(p.info.get('cmdline') or []).lower()
                        if desktop_name:
                            if norm_cdp_data in cmd_str:
                                running_procs.append(p)
                        else:
                            running_procs.append(p)
                except Exception:
                    pass
            if running_procs:
                logger.info(f"Terminating {len(running_procs)} stale {browser} processes to free CDP port...")
                for p in running_procs:
                    try:
                        p.kill()
                    except Exception:
                        pass
                time.sleep(0.8)
        except Exception as e:
            logger.warning(f"Error checking running browser processes: {e}")

        flags = [
            f"--remote-debugging-port={self.port}",
            f"--remote-debugging-address={self.host}",
            "--remote-allow-origins=*",
            f"--user-data-dir={cdp_data}",
            f"--profile-directory={last_profile}",
            "--no-profile-picker",
            "--restore-last-session",
            "--no-first-run",
            "--no-default-browser-check",
            "--start-maximized",
            "https://www.google.com"
        ]

        try:
            logger.info(f"Auto-launching {browser} with profile '{last_profile}' and CDP port {self.port} (desktop: '{desktop_name}')...")
            if desktop_name:
                from .spawner import AppSpawner
                spawner = AppSpawner(desktop_name=desktop_name)
                if browser == "edge":
                    pid, err, _ = spawner.launch_edge("https://www.bing.com")
                else:
                    pid, err, _ = spawner.launch_chrome("https://www.google.com")
                if not pid:
                    logger.error(f"Failed to spawn {browser} on desktop '{desktop_name}': {err}")
                    return False
                logger.info(f"Spawned {browser} (PID: {pid}) inside Backstage desktop '{desktop_name}' (100% stealth, zero target monitor leakage).")
            else:
                cmd = [binary] + flags
                subprocess.Popen(cmd, creationflags=0x00000208, close_fds=True)
        except Exception as e:
            logger.error(f"Failed to spawn {browser} for CDP: {e}")
            return False

        # Wait for port to become active
        start_t = time.time()
        while time.time() - start_t < timeout:
            if self.is_port_open():
                logger.info(f"CDP port {self.port} is now open and listening!")
                return True
            time.sleep(0.4)

        return self.is_port_open()


    def get_browser_version(self) -> Optional[Dict[str, Any]]:
        """Retrieves browser name, user-agent, and webSocketDebuggerUrl for the browser target."""
        try:
            req = urllib.request.Request(f"{self.base_url}/json/version", headers={"User-Agent": "TRMM-CDP-Client"})
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception:
            return None

    async def connect_to_tab(self, tab_id: Optional[str] = None) -> bool:
        """
        Connects via WebSocket to a specific tab target. If no tab_id is specified,
        connects to the first available active page tab.
        """
        import websockets

        tabs = self.list_tabs()
        if not tabs:
            # Auto-create a tab if browser has no page tabs open yet
            try:
                req = urllib.request.Request(f"{self.base_url}/json/new?https://www.google.com", method="PUT", headers={"User-Agent": "TRMM-CDP-Client"})
                with urllib.request.urlopen(req, timeout=2.0) as resp:
                    pass
                await asyncio.sleep(0.6)
                tabs = self.list_tabs()
            except Exception as e:
                logger.debug(f"Failed to create new tab: {e}")

        if not tabs:
            logger.warning("No inspectable page tabs found via CDP.")
            return False

        target_tab = None
        if tab_id:
            for t in tabs:
                if t.get("id") == tab_id:
                    target_tab = t
                    break

        if not target_tab:
            target_tab = tabs[0]

        self.active_tab_id = target_tab.get("id")
        self.ws_url = target_tab.get("webSocketDebuggerUrl")

        if not self.ws_url:
            logger.warning("Target tab does not have a webSocketDebuggerUrl.")
            return False

        if self.ws:
            try:
                await self.ws.close()
            except Exception:
                pass
            self.ws = None

        try:
            self.ws = await websockets.connect(self.ws_url, max_size=10 * 1024 * 1024)
            logger.info(f"CDP connected to tab: '{target_tab.get('title')}' ({target_tab.get('url')})")
            return True
        except Exception as e:
            logger.error(f"CDP WebSocket connection failed: {e}")
            return False

    @staticmethod
    def is_ws_open(ws) -> bool:
        """Determines if a WebSocket connection is active across all websockets versions."""
        if ws is None:
            return False
        if hasattr(ws, "open"):
            try:
                return bool(ws.open)
            except Exception:
                pass
        if hasattr(ws, "state"):
            try:
                return getattr(ws.state, "name", "") == "OPEN"
            except Exception:
                pass
        return True

    async def send_command(self, method: str, params: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        """Sends a JSON-RPC command over the CDP WebSocket and returns the result."""
        if not self.ws or not self.is_ws_open(self.ws):
            return None

        self._command_id += 1
        cmd_id = self._command_id
        payload = {"id": cmd_id, "method": method, "params": params or {}}

        try:
            await self.ws.send(json.dumps(payload))
            return {"id": cmd_id, "status": "sent"}
        except Exception as e:
            logger.error(f"CDP send_command error: {e}")
            return None

    async def start_screencast(self, format: str = "jpeg", quality: int = 80, max_width: int = 1280, max_height: int = 720):
        """Requests Chrome to begin streaming screencast frames."""
        self.is_streaming = True
        await self.send_command("Page.enable")
        await self.send_command("Page.bringToFront")
        params = {
            "format": format,
            "quality": quality,
            "maxWidth": max_width,
            "maxHeight": max_height,
            "everyNthFrame": 1,
        }
        await self.send_command("Page.startScreencast", params)

        # Trigger immediate screenshot request so viewer receives an instantaneous frame
        try:
            await self.send_command("Page.captureScreenshot", {"format": format, "quality": quality})
        except Exception as e:
            logger.debug(f"CDP initial screenshot trigger note: {e}")

    async def stop_screencast(self):
        """Stops the screencast stream."""
        self.is_streaming = False
        await self.send_command("Page.stopScreencast")

    async def dispatch_mouse(self, event_type: str, x: int, y: int, button: str = "none", click_count: int = 0):
        """
        Dispatches mouse events to the tab viewport.
        event_type: 'mousePressed', 'mouseReleased', 'mouseMoved', 'mouseWheel'
        """
        params: Dict[str, Any] = {
            "type": event_type,
            "x": x,
            "y": y,
            "button": button,
            "clickCount": click_count,
        }
        await self.send_command("Input.dispatchMouseEvent", params)

    async def dispatch_wheel(self, x: int, y: int, delta_x: int = 0, delta_y: int = 0):
        """Dispatches mouse wheel scroll events."""
        params = {
            "type": "mouseWheel",
            "x": x,
            "y": y,
            "deltaX": delta_x,
            "deltaY": delta_y,
        }
        await self.send_command("Input.dispatchMouseEvent", params)

    async def dispatch_key(self, event_type: str, key: str = "", code: str = "", text: str = "", windows_virtual_key_code: int = 0):
        """
        Dispatches keyboard events to the tab.
        event_type: 'keyDown', 'keyUp', 'rawKeyDown', 'char'
        """
        params: Dict[str, Any] = {
            "type": event_type,
            "key": key,
            "code": code,
            "windowsVirtualKeyCode": windows_virtual_key_code,
        }
        if text:
            params["text"] = text
        await self.send_command("Input.dispatchKeyEvent", params)

    async def navigate(self, url: str):
        """Navigates active tab to a specified URL."""
        await self.send_command("Page.navigate", {"url": url})

    async def listen_loop(self):
        """
        Event pump listening for incoming CDP events (like Page.screencastFrame)
        and dispatching acknowledgements.
        """
        import base64

        while self.ws and self.is_ws_open(self.ws) and self.is_streaming:
            try:
                msg_raw = await self.ws.recv()
                msg = json.loads(msg_raw)

                # 1. Handle direct screenshot capture response
                if "result" in msg and "data" in msg["result"] and self.frame_callback:
                    b64_data = msg["result"]["data"]
                    if b64_data:
                        try:
                            img_bytes = base64.b64decode(b64_data)
                            self.frame_callback(img_bytes)
                        except Exception:
                            pass

                method = msg.get("method")
                if method == "Page.screencastFrame":
                    params = msg.get("params", {})
                    session_id = params.get("sessionId")
                    b64_data = params.get("data", "")

                    # Acknowledge frame to keep Chrome sending frames
                    if session_id:
                        await self.send_command("Page.screencastFrameAck", {"sessionId": session_id})

                    if b64_data and self.frame_callback:
                        img_bytes = base64.b64decode(b64_data)
                        self.frame_callback(img_bytes)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"CDP message pump error: {e}")
                break

    async def close(self):
        """Disconnects the CDP WebSocket session."""
        self.is_streaming = False
        if self.ws:
            try:
                await self.ws.close()
            except Exception:
                pass
            self.ws = None
