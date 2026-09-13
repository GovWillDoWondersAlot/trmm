"""
FastAPI + WebSocket Streaming Server for HVNC.
Provides high-performance live frame streaming and remote mouse/keyboard control over WebSockets.
"""

import asyncio
import base64
import json
import logging
import os
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from .desktop import HiddenDesktop
from .spawner import AppSpawner
from .compositor import WindowCompositor
from .input_handler import InputHandler

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("hvnc.server")

app = FastAPI(title="TRMM Python HVNC Server", version="1.0.0")

# Global instances
desktop = HiddenDesktop("TRMM_Hidden_Workspace")
spawner = AppSpawner("TRMM_Hidden_Workspace")
compositor = WindowCompositor(desktop, width=1280, height=720)
input_handler = InputHandler(desktop)

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class LaunchRequest(BaseModel):
    app_name: str
    url: Optional[str] = None
    custom_path: Optional[str] = None
    args: Optional[str] = None


@app.on_event("startup")
async def startup_event():
    logger.info("Initializing Hidden Desktop workspace...")
    desktop.initialize()


@app.on_event("shutdown")
async def shutdown_event():
    logger.info("Closing Hidden Desktop workspace...")
    desktop.close()


@app.get("/")
async def get_index():
    index_path = os.path.join(STATIC_DIR, "index.html")
    if os.path.isfile(index_path):
        return FileResponse(index_path)
    return JSONResponse({"status": "HVNC active", "message": "Static assets not found."})


@app.get("/api/status")
async def get_status():
    windows = desktop.enumerate_windows()
    win_list = [{"hwnd": w[0], "title": w[1], "class": w[2], "rect": w[3]} for w in windows]
    return {
        "status": "online",
        "desktop_name": desktop.desktop_name,
        "resolution": {"width": compositor.width, "height": compositor.height},
        "windows_count": len(windows),
        "windows": win_list,
    }


@app.post("/api/launch")
async def launch_app(req: LaunchRequest):
    app_name = req.app_name.lower()
    pid = None

    if app_name == "chrome":
        pid = spawner.launch_chrome(req.url or "https://google.com")
    elif app_name == "edge":
        pid = spawner.launch_edge(req.url or "https://bing.com")
    elif app_name == "firefox":
        pid = spawner.launch_firefox(req.url or "https://mozilla.org")
    elif app_name == "explorer":
        pid = spawner.launch_explorer()
    elif app_name == "cmd":
        pid = spawner.launch_cmd()
    elif app_name == "powershell":
        pid = spawner.launch_powershell()
    elif app_name == "regedit":
        pid = spawner.launch_regedit()
    elif app_name == "taskmgr":
        pid = spawner.launch_taskmgr()
    elif app_name == "notepad":
        pid = spawner.launch_notepad()
    elif app_name == "custom" and req.custom_path:
        cmd = f'"{req.custom_path}" {req.args or ""}'
        pid = spawner.spawn_raw(None, cmd)

    if pid:
        return {"success": True, "pid": pid, "app": app_name}
    return {"success": False, "error": f"Failed to launch '{app_name}'"}


@app.websocket("/ws/stream")
async def websocket_stream(websocket: WebSocket):
    await websocket.accept()
    logger.info("WebSocket client connected to HVNC stream.")

    target_fps = 20
    frame_interval = 1.0 / target_fps
    is_running = True

    async def send_frames():
        loop = asyncio.get_event_loop()
        import time as _time
        while is_running:
            try:
                t0 = _time.perf_counter()
                # Render frame in worker thread to avoid blocking asyncio event loop
                frame_bytes = await loop.run_in_executor(None, compositor.render_frame)
                await websocket.send_bytes(frame_bytes)
                elapsed = _time.perf_counter() - t0
                sleep_time = max(0.005, frame_interval - elapsed)
                await asyncio.sleep(sleep_time)
            except Exception as e:
                logger.debug(f"Frame streaming ended: {e}")
                break

    stream_task = asyncio.create_task(send_frames())

    try:
        while True:
            data = await websocket.receive_text()
            try:
                msg = json.loads(data)
                msg_type = msg.get("type")

                if msg_type == "mousemove":
                    x = int(msg.get("x", 0))
                    y = int(msg.get("y", 0))
                    compositor.set_cursor_pos(x, y)
                    input_handler.handle_mouse_move(x, y)

                elif msg_type == "mousedown":
                    x = int(msg.get("x", 0))
                    y = int(msg.get("y", 0))
                    btn = msg.get("button", "left")
                    compositor.set_cursor_pos(x, y)
                    input_handler.handle_mouse_down(x, y, btn)

                elif msg_type == "mouseup":
                    x = int(msg.get("x", 0))
                    y = int(msg.get("y", 0))
                    btn = msg.get("button", "left")
                    compositor.set_cursor_pos(x, y)
                    input_handler.handle_mouse_up(x, y, btn)

                elif msg_type == "dblclick":
                    x = int(msg.get("x", 0))
                    y = int(msg.get("y", 0))
                    input_handler.handle_double_click(x, y)

                elif msg_type == "wheel":
                    x = int(msg.get("x", 0))
                    y = int(msg.get("y", 0))
                    delta = int(msg.get("delta", 0))
                    input_handler.handle_mouse_wheel(x, y, delta)

                elif msg_type == "keydown":
                    vk = int(msg.get("vk", 0))
                    input_handler.handle_key_down(vk)

                elif msg_type == "keyup":
                    vk = int(msg.get("vk", 0))
                    input_handler.handle_key_up(vk)

                elif msg_type == "char":
                    char_code = int(msg.get("char", 0))
                    input_handler.handle_char(char_code)

                elif msg_type == "launch":
                    app_to_launch = msg.get("app", "")
                    url = msg.get("url")
                    if app_to_launch == "chrome":
                        spawner.launch_chrome(url or "https://google.com")
                    elif app_to_launch == "edge":
                        spawner.launch_edge(url or "https://bing.com")
                    elif app_to_launch == "explorer":
                        spawner.launch_explorer()
                    elif app_to_launch == "cmd":
                        spawner.launch_cmd()
                    elif app_to_launch == "powershell":
                        spawner.launch_powershell()
                    elif app_to_launch == "regedit":
                        spawner.launch_regedit()
                    elif app_to_launch == "taskmgr":
                        spawner.launch_taskmgr()

            except json.JSONDecodeError:
                pass
    except WebSocketDisconnect:
        logger.info("WebSocket client disconnected.")
    finally:
        is_running = False
        stream_task.cancel()
