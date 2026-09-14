"""
Tactical RMM — Master Admin Hub Server.
Provides central Web Dashboard, Agent Generator, Live Telemetry, and WebSocket Reverse Tunnel Relay.
"""

import os
import sys
import json
import uuid
import logging
import threading
from pathlib import Path
import yaml
from typing import Dict, Any, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Response, Request, Cookie
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from .agent_manager import AgentManager
from .generator import AgentGenerator, OUTPUT_DIR
from .ip_watcher import watch_ip
from .ota_manager import OTAManager
from .auth import AuthManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("master_hub.server")

# Load public IP configuration if exists
CONFIG_PATH = Path(__file__).parent / "config.yaml"

def get_default_server_url() -> str:
    if CONFIG_PATH.is_file():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            public_ip = cfg.get("public_ip")
            if public_ip:
                return f"ws://{public_ip}:8000"
        except Exception:
            pass
    return "ws://127.0.0.1:8000"

# Start background IP watcher thread
threading.Thread(target=watch_ip, daemon=True).start()

app = FastAPI(title="Tactical RMM Master Hub", version="2.0.0")

agent_mgr = AgentManager()

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

dashboard_websockets = set()


def _is_authenticated(request: Request) -> bool:
    """Checks session validity from Cookie or Authorization header."""
    token = request.cookies.get("trmm_session")
    if not token:
        auth_hdr = request.headers.get("Authorization", "")
        if auth_hdr.startswith("Bearer "):
            token = auth_hdr.split(" ", 1)[1].strip()
    return AuthManager.validate_session(token)


class AuthLoginRequest(BaseModel):
    username: Optional[str] = ""
    password: Optional[str] = ""
    master_key: Optional[str] = None


class AuthSetupRequest(BaseModel):
    username: str
    password: str


# -------------------------------------------------------------
# Admin Authentication & Setup Endpoints
# -------------------------------------------------------------
@app.get("/login")
async def get_login_page(request: Request):
    # If already logged in, redirect straight to dashboard
    if _is_authenticated(request):
        return RedirectResponse(url="/", status_code=302)

    login_path = os.path.join(STATIC_DIR, "login.html")
    if os.path.isfile(login_path):
        return FileResponse(login_path, headers={"Cache-Control": "no-cache, no-store, must-revalidate"})
    return JSONResponse({"status": "Login Page Missing"})


@app.get("/api/auth/status")
async def get_auth_status(request: Request):
    return {
        "setup_required": AuthManager.is_setup_required(),
        "is_authenticated": _is_authenticated(request)
    }


@app.post("/api/auth/setup")
async def post_auth_setup(req: AuthSetupRequest):
    if not AuthManager.is_setup_required():
        raise HTTPException(status_code=400, detail="System is already configured. Please log in.")

    res = AuthManager.setup_admin(req.username, req.password)
    if not res.get("success"):
        raise HTTPException(status_code=400, detail=res.get("error", "Setup failed"))

    response = JSONResponse({"success": True, "message": "Admin account initialized"})
    response.set_cookie(
        key="trmm_session",
        value=res["token"],
        max_age=8 * 3600,
        httponly=True,
        samesite="lax"
    )
    return response


@app.post("/api/auth/login")
async def post_auth_login(req: AuthLoginRequest):
    res = AuthManager.verify_login(
        username=req.username or "",
        password=req.password or "",
        master_key=req.master_key
    )
    if not res.get("success"):
        raise HTTPException(status_code=401, detail=res.get("error", "Invalid credentials"))

    response = JSONResponse({
        "success": True,
        "message": "Authenticated successfully",
        "username": res.get("username"),
        "is_master_key": res.get("is_master_key", False)
    })
    response.set_cookie(
        key="trmm_session",
        value=res["token"],
        max_age=8 * 3600,
        httponly=True,
        samesite="lax"
    )
    return response


@app.post("/api/auth/logout")
async def post_auth_logout(request: Request):
    token = request.cookies.get("trmm_session")
    AuthManager.destroy_session(token)
    response = JSONResponse({"success": True, "message": "Logged out"})
    response.delete_cookie(key="trmm_session")
    return response


async def broadcast_agent_list():
    """Broadcasts updated agent list to all open admin dashboard tabs."""
    if not dashboard_websockets:
        return
    agents = agent_mgr.list_all_agents()
    payload = json.dumps({"type": "agents_update", "agents": agents})
    dead = set()
    for ws in dashboard_websockets:
        try:
            await ws.send_text(payload)
        except Exception:
            dead.add(ws)
    dashboard_websockets.difference_update(dead)


class GenerateAgentRequest(BaseModel):
    endpoint_tag: str = "Workstation-01"
    arch: str = "x64"  # x64 or x86
    server_url: str = get_default_server_url()
    auto_start: bool = True
    hidden_mode: bool = True
    custom_name: Optional[str] = None
    icon_base64: Optional[str] = None


@app.get("/")
async def get_index(request: Request):
    if not _is_authenticated(request):
        return RedirectResponse(url="/login", status_code=302)

    index_path = os.path.join(STATIC_DIR, "index.html")
    if os.path.isfile(index_path):
        return FileResponse(index_path, headers={"Cache-Control": "no-cache, no-store, must-revalidate"})
    return JSONResponse({"status": "Tactical RMM Master Hub Online"})


@app.get("/viewer/{agent_id}")
async def get_viewer_page(request: Request, agent_id: str):
    if not _is_authenticated(request):
        return RedirectResponse(url="/login", status_code=302)

    viewer_path = os.path.join(STATIC_DIR, "viewer.html")
    if os.path.isfile(viewer_path):
        return FileResponse(viewer_path, headers={"Cache-Control": "no-cache, no-store, must-revalidate"})
    index_path = os.path.join(STATIC_DIR, "index.html")
    return FileResponse(index_path, headers={"Cache-Control": "no-cache, no-store, must-revalidate"})


import socket

@app.get("/api/system/info")
async def get_system_info(request: Request):
    """Returns Master Hub network information and host IPs for easy agent generation."""
    if not _is_authenticated(request):
        raise HTTPException(status_code=401, detail="Authentication required")

    local_ips = []
    try:
        host_name = socket.gethostname()
        for ip in socket.gethostbyname_ex(host_name)[2]:
            if not ip.startswith("127."):
                local_ips.append(ip)
    except Exception:
        pass
    if not local_ips:
        local_ips = ["127.0.0.1"]
    return {
        "hostname": socket.gethostname(),
        "local_ips": local_ips,
        "default_port": 8000
    }


@app.get("/api/agents")
async def get_agents(request: Request):
    if not _is_authenticated(request):
        raise HTTPException(status_code=401, detail="Authentication required")
    return {"agents": agent_mgr.list_all_agents()}


@app.delete("/api/agents/{agent_id}")
async def delete_agent(request: Request, agent_id: str):
    if not _is_authenticated(request):
        raise HTTPException(status_code=401, detail="Authentication required")
    removed = agent_mgr.remove_agent(agent_id)
    if not removed:
        raise HTTPException(status_code=404, detail="Agent not found")
    await broadcast_agent_list()
    return {"success": True, "message": f"Agent {agent_id} removed"}


@app.post("/api/agents/generate")
async def generate_agent(request: Request, req: GenerateAgentRequest):
    if not _is_authenticated(request):
        raise HTTPException(status_code=401, detail="Authentication required")
    try:
        cfg = req.dict()
        cfg["agent_id"] = str(uuid.uuid4())[:8]
        
        # If server_url was not customized or is default localhost, dynamically adapt to the Host used to access the hub
        server_url = (cfg.get("server_url") or "").strip()
        if not server_url or "127.0.0.1:8000" in server_url or "localhost:8000" in server_url:
            client_host = request.headers.get("x-forwarded-host") or request.headers.get("host") or "127.0.0.1:8000"
            scheme = request.headers.get("x-forwarded-proto") or request.url.scheme
            ws_scheme = "wss" if scheme == "https" else "ws"
            cfg["server_url"] = f"{ws_scheme}://{client_host}"
            
        result = AgentGenerator.build_package(cfg)
        return {
            "success": True,
            "agent_id": cfg["agent_id"],
            "exe_filename": result.get("exe_filename"),
            "exe_download_url": result.get("exe_download_url"),
            "zip_filename": result.get("zip_filename"),
            "zip_download_url": result.get("zip_download_url"),
            "bootstrap_filename": result.get("bootstrap_filename"),
            "bootstrap_download_url": result.get("bootstrap_download_url"),
            "one_liner": result.get("one_liner"),
        }
    except Exception as e:
        logger.error(f"Failed to generate agent package: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/agents/bootstrap/{filename}")
async def download_bootstrap_script(filename: str):
    file_path = os.path.join(OUTPUT_DIR, filename)
    if not os.path.isfile(file_path):
        raise HTTPException(status_code=404, detail="Bootstrap script not found")
    return FileResponse(
        file_path,
        filename=filename,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/agents/download/{filename}")
async def download_agent(filename: str):
    file_path = os.path.join(OUTPUT_DIR, filename)
    if not os.path.isfile(file_path):
        raise HTTPException(status_code=404, detail="File not found")

    return FileResponse(
        file_path,
        filename=filename,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# -------------------------------------------------------------
# OTA Live Code & Dependency Provisioning Endpoints
# -------------------------------------------------------------
@app.get("/api/agent/code_manifest")
async def get_code_manifest():
    return JSONResponse(OTAManager.get_code_manifest())


@app.get("/api/agent/code_bundle")
async def get_code_bundle():
    bundle_bytes, bundle_hash = OTAManager.generate_code_bundle()
    return Response(
        content=bundle_bytes,
        media_type="application/zip",
        headers={
            "Content-Disposition": 'attachment; filename="code_bundle.zip"',
            "X-Code-Hash": bundle_hash
        }
    )


@app.get("/api/agent/payloads/{filename}")
async def get_agent_payload(filename: str):
    p = OTAManager.get_payload_path(filename)
    if not p:
        raise HTTPException(status_code=404, detail="Payload not found")
    return FileResponse(p, filename=os.path.basename(p))


@app.post("/api/agent/{agent_id}/sync")
async def trigger_agent_sync(request: Request, agent_id: str):
    if not _is_authenticated(request):
        raise HTTPException(status_code=401, detail="Authentication required")
    agent = agent_mgr.active_agents.get(agent_id)
    if not agent or not agent.ws:
        raise HTTPException(status_code=404, detail="Agent offline or not connected")

    server_manifest = OTAManager.get_code_manifest()
    server_hash = server_manifest.get("overall_hash", "")
    agent_hash = agent.client_info.get("code_hash", "")
    description = server_manifest.get("description", "Latest code features applied.")

    has_update = (agent_hash != server_hash)
    try:
        await agent.ws.send_text(json.dumps({
            "type": "sync_code",
            "server_hash": server_hash,
            "description": description,
            "force": True,
            "force_restart": has_update
        }))
        return JSONResponse({
            "status": "update_applied" if has_update else "no_update",
            "has_update": has_update,
            "message": f"Update applied: {description}" if has_update else "No update available. Device is already running the latest version.",
            "description": description,
            "agent_id": agent_id,
            "server_hash": server_hash,
            "agent_hash": agent_hash
        })
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to dispatch sync: {e}")


class InstallServiceRequest(BaseModel):
    name: str
    display_name: Optional[str] = None
    bin_path: str
    start_type: str = "auto"


@app.post("/api/agent/{agent_id}/install_service")
async def trigger_install_service(request: Request, agent_id: str, req: InstallServiceRequest):
    if not _is_authenticated(request):
        raise HTTPException(status_code=401, detail="Authentication required")
    agent = agent_mgr.get_agent(agent_id)
    if not agent or not agent.ws:
        raise HTTPException(status_code=404, detail="Agent offline or not connected")
    try:
        await agent.ws.send_text(json.dumps({
            "type": "install_service",
            "name": req.name,
            "display_name": req.display_name or req.name,
            "bin_path": req.bin_path,
            "start_type": req.start_type
        }))
        return JSONResponse({"status": "service_install_dispatched", "agent_id": agent_id})
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to dispatch service install: {e}")


class InstallPackageRequest(BaseModel):
    package_url: str
    silent_flags: Optional[str] = None


@app.post("/api/agent/{agent_id}/install_package")
async def trigger_install_package(request: Request, agent_id: str, req: InstallPackageRequest):
    if not _is_authenticated(request):
        raise HTTPException(status_code=401, detail="Authentication required")
    agent = agent_mgr.get_agent(agent_id)
    if not agent or not agent.ws:
        raise HTTPException(status_code=404, detail="Agent offline or not connected")
    try:
        await agent.ws.send_text(json.dumps({
            "type": "install_package",
            "package_url": req.package_url,
            "silent_flags": req.silent_flags
        }))
        return JSONResponse({"status": "package_install_dispatched", "agent_id": agent_id})
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to dispatch package install: {e}")


@app.post("/api/agent/{agent_id}/restart")
async def trigger_agent_restart(request: Request, agent_id: str):
    if not _is_authenticated(request):
        raise HTTPException(status_code=401, detail="Authentication required")
    agent = agent_mgr.get_agent(agent_id)
    if not agent or not agent.ws:
        raise HTTPException(status_code=404, detail="Agent offline or not connected")
    try:
        await agent.ws.send_text(json.dumps({"type": "restart_agent"}))
        return JSONResponse({"status": "restart_dispatched", "agent_id": agent_id})
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to dispatch restart: {e}")


# -------------------------------------------------------------
# WebSocket: Live Dashboard Updates
# -------------------------------------------------------------
@app.websocket("/ws/dashboard")
async def websocket_dashboard(websocket: WebSocket):
    await websocket.accept()
    dashboard_websockets.add(websocket)
    try:
        # Send initial list
        agents = agent_mgr.list_all_agents()
        await websocket.send_text(json.dumps({"type": "agents_update", "agents": agents}))
        while True:
            await websocket.receive_text()
    except (WebSocketDisconnect, RuntimeError):
        pass
    except Exception:
        pass
    finally:
        dashboard_websockets.discard(websocket)


# -------------------------------------------------------------
# WebSocket: Reverse Agent Connection Tunnel
# -------------------------------------------------------------
@app.websocket("/ws/agent/{agent_id}")
@app.websocket("/ws/ws/agent/{agent_id}")
async def websocket_agent_tunnel(websocket: WebSocket, agent_id: str):
    await websocket.accept()

    # If agent was previously decommissioned, clear the flag on fresh manual reconnect
    if agent_id in agent_mgr.decommissioned_agents:
        agent_mgr.decommissioned_agents.discard(agent_id)
        logger.info(f"Agent {agent_id} re-registered after previous decommissioning.")

    agent_info = {}
    registered = False

    try:
        # Wait for handshake
        raw_handshake = await websocket.receive_text()
        handshake_data = json.loads(raw_handshake)
        if handshake_data.get("type") == "handshake":
            agent_info = handshake_data.get("data", {})

        agent = agent_mgr.register_connection(agent_id, websocket, agent_info)
        if not agent:
            try:
                await websocket.send_text(json.dumps({
                    "type": "terminate",
                    "reason": "Another agent instance is already active or agent decommissioned",
                    "uninstall": (agent_id in agent_mgr.decommissioned_agents)
                }))
                await websocket.close(code=4009, reason="Duplicate or decommissioned endpoint session rejected")
            except Exception:
                pass
            return

        registered = True
        # Note: Do not auto-update immediately upon connecting.
        # The admin interface will display an "Update Available" indicator/badge
        # and allow the admin to review and push the update intentionally.
        server_manifest = OTAManager.get_code_manifest()
        server_hash = server_manifest.get("overall_hash")
        agent_hash = agent_info.get("code_hash", "")
        if agent_hash != server_hash:
            logger.info(f"Agent {agent_id} code hash '{agent_hash[:8]}' differs from server '{server_hash[:8]}'. Update available for admin dispatch.")

        while True:
            # Receive either binary frame data or text telemetry/heartbeat
            msg = await websocket.receive()
            if "bytes" in msg and msg["bytes"]:
                frame_data = msg["bytes"]
                if len(frame_data) > 1 and frame_data[0] == 1:
                    # Explicit Backstage frame
                    raw_frame = frame_data[1:]
                    for v_ws in list(agent.viewers.values()):
                        try:
                            await v_ws.send_bytes(raw_frame)
                        except Exception:
                            pass
                elif len(frame_data) > 1 and frame_data[0] == 2:
                    # Explicit Screen Mirror frame
                    raw_frame = frame_data[1:]
                    for v_ws in list(agent.mirror_viewers.values()):
                        try:
                            await v_ws.send_bytes(raw_frame)
                        except Exception:
                            pass
                elif len(frame_data) > 1 and frame_data[0] == 3:
                    # Explicit CDP Screencast browser frame (both Backstage & Mirror viewers can render)
                    raw_frame = frame_data[1:]
                    all_viewers = list(agent.viewers.values()) + list(agent.mirror_viewers.values())
                    for v_ws in all_viewers:
                        try:
                            await v_ws.send_bytes(raw_frame)
                        except Exception:
                            pass
                else:
                    # Untagged frame routing
                    if agent.mirror_viewers and not agent.viewers:
                        target_viewers = list(agent.mirror_viewers.values())
                    elif agent.viewers and not agent.mirror_viewers:
                        target_viewers = list(agent.viewers.values())
                    elif agent.is_streaming_mirror:
                        target_viewers = list(agent.mirror_viewers.values())
                    elif agent.is_streaming_hvnc:
                        target_viewers = list(agent.viewers.values())
                    else:
                        target_viewers = list(agent.viewers.values()) + list(agent.mirror_viewers.values())

                    for v_ws in target_viewers:
                        try:
                            await v_ws.send_bytes(frame_data)
                        except Exception:
                            pass

            elif "text" in msg and msg["text"]:
                text_data = json.loads(msg["text"])
                if text_data.get("type") == "heartbeat":
                    agent.update_info(text_data.get("data", {}))
                    await broadcast_agent_list()
                else:
                    # Forward agent diagnostics (launch status, click hit tests, errors) to viewers
                    raw_text = msg["text"]
                    all_viewers = list(agent.viewers.values()) + list(agent.mirror_viewers.values())
                    for v_ws in all_viewers:
                        try:
                            await v_ws.send_text(raw_text)
                        except Exception:
                            pass

    except (WebSocketDisconnect, RuntimeError):
        logger.info(f"Agent {agent_id} disconnected.")
    except Exception as e:
        logger.debug(f"Agent {agent_id} tunnel closed: {e}")
    finally:
        if registered:
            agent_mgr.unregister_connection(agent_id)
            await broadcast_agent_list()


# -------------------------------------------------------------
# WebSocket: Admin Remote Viewer (HVNC or Screen Mirror)
# -------------------------------------------------------------
@app.websocket("/ws/viewer/{agent_id}")
async def websocket_admin_viewer(websocket: WebSocket, agent_id: str):
    await websocket.accept()
    mode = websocket.query_params.get("mode", "backstage").lower()
    viewer_id = str(uuid.uuid4())[:8]

    agent = agent_mgr.get_agent(agent_id)
    if not agent:
        await websocket.send_text(json.dumps({"type": "error", "message": "Agent is currently offline"}))
        await websocket.close()
        return

    agent_mgr.attach_viewer(agent_id, viewer_id, websocket, mode=mode)

    # Tell the agent to start appropriate stream (Mirror vs HVNC Backstage)
    try:
        if mode == "mirror":
            await agent.ws.send_text(json.dumps({"type": "start_mirror"}))
            agent.is_streaming_mirror = True
        else:
            await agent.ws.send_text(json.dumps({"type": "start_hvnc"}))
            agent.is_streaming_hvnc = True
        await broadcast_agent_list()
    except Exception as e:
        logger.error(f"Failed to signal agent to start {mode} stream: {e}")

    try:
        while True:
            # Forward mouse/keyboard and launch commands from admin browser to the target agent
            data = await websocket.receive_text()
            try:
                msg_obj = json.loads(data)
                if isinstance(msg_obj, dict) and "mode" not in msg_obj:
                    msg_obj["mode"] = mode
                    if "data" in msg_obj and isinstance(msg_obj["data"], dict) and "mode" not in msg_obj["data"]:
                        msg_obj["data"]["mode"] = mode
                    data = json.dumps(msg_obj)
            except Exception:
                pass
            try:
                await agent.ws.send_text(data)
            except Exception:
                break
    except (WebSocketDisconnect, RuntimeError):
        pass
    except Exception:
        pass
    finally:
        agent_mgr.detach_viewer(agent_id, viewer_id, mode=mode)
        # If no viewers remain for this mode, tell agent to stop stream to save CPU
        if mode == "mirror":
            if not agent.mirror_viewers:
                agent.is_streaming_mirror = False
                try:
                    await agent.ws.send_text(json.dumps({"type": "stop_mirror"}))
                except Exception:
                    pass
                await broadcast_agent_list()
        else:
            if not agent.viewers:
                agent.is_streaming_hvnc = False
                try:
                    await agent.ws.send_text(json.dumps({"type": "stop_hvnc"}))
                except Exception:
                    pass
                await broadcast_agent_list()


# -------------------------------------------------------------
# GitHub Webhook Auto-Deployment Endpoint
# -------------------------------------------------------------
@app.post("/api/webhook/github")
async def github_webhook(request: Request):
    """
    Receives push event from GitHub, pulls latest changes into /opt/trmm,
    and restarts trmm.service so changes take effect live instantly.
    """
    event = request.headers.get("X-GitHub-Event", "ping")
    if event == "ping":
        logger.info("[GitHub Webhook] Received ping event.")
        return {"status": "pong"}

    if event == "push":
        logger.info("[GitHub Webhook] Received push event. Triggering automated deployment...")

        def run_update():
            time.sleep(1)
            cmd = "cd /opt/trmm && git fetch --all && git reset --hard origin/main && sudo systemctl restart trmm"
            res = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True)
            logger.info(f"[GitHub Webhook] Deploy output: {res.stdout}, errors: {res.stderr}")

        threading.Thread(target=run_update, daemon=True).start()
        return {"status": "deployment_triggered"}

    return {"status": "ignored", "event": event}
