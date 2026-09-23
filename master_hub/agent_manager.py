"""
Master Hub Agent Manager.
Tracks online/offline endpoints, system telemetry, and coordinates reverse WebSocket sessions.
"""

import time
import json
import logging
import os
from .mirror_delivery import LowLatencyMirrorViewerSession
from typing import Dict, Any, Optional
from fastapi import WebSocket

logger = logging.getLogger("master_hub.agent_manager")


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
        # Lock endpoint_tag: preserve established tag so heartbeats can never alter it
        current_tag = self.client_info.get("endpoint_tag")
        if current_tag:
            info["endpoint_tag"] = current_tag
        self.client_info.update(info)
        self.last_heartbeat = time.time()

    def to_dict(self) -> Dict[str, Any]:
        from .ota_manager import OTAManager
        server_manifest = OTAManager.get_code_manifest()
        server_hash = server_manifest.get("overall_hash", "")
        agent_hash = self.client_info.get("code_hash", "")
        has_update = bool(server_hash and agent_hash and server_hash != agent_hash)

        return {
            "agent_id": self.agent_id,
            "device_id": self.client_info.get("device_id", ""),
            "installation_id": self.client_info.get("installation_id", ""),
            "endpoint_tag": self.client_info.get("endpoint_tag", self.client_info.get("tag", "")),
            "status": "online",
            "hostname": self.client_info.get("hostname", "Unknown"),
            "username": self.client_info.get("username", "Unknown"),
            "os": self.client_info.get("os", "Windows"),
            "arch": self.client_info.get("arch", "x64"),
            "ram_gb": self.client_info.get("ram_gb", 0),
            "cpu": self.client_info.get("cpu", "Unknown"),
            "local_ip": self.client_info.get("local_ip", "127.0.0.1"),
            "public_ip": self.client_info.get("public_ip", ""),
            "connected_at": self.connected_at,
            "last_heartbeat": self.last_heartbeat,
            "is_streaming": self.is_streaming_hvnc,
            "is_mirroring": self.is_streaming_mirror,
            "code_hash": agent_hash,
            "server_hash": server_hash,
            "has_update": has_update,
            "update_version": server_manifest.get("version", "2.8.0-live"),
            "update_description": server_manifest.get("description", "A new OTA update is available.")
        }


class AgentManager:
    """Central registry for all active endpoint connections and active remote sessions."""

    def __init__(self):
        self.active_agents: Dict[str, ConnectedAgent] = {}
        self.registered_agents: Dict[str, Dict[str, Any]] = {}
        self.decommissioned_agents: set = set()

    def set_agent_tag(self, agent_id: str, new_tag: str):
        """Allows administrator to intentionally rename an endpoint tag."""
        if agent_id in self.active_agents:
            self.active_agents[agent_id].client_info["endpoint_tag"] = new_tag
        if agent_id in self.registered_agents:
            self.registered_agents[agent_id]["endpoint_tag"] = new_tag

    def register_connection(self, agent_id: str, ws: WebSocket, info: Dict[str, Any]) -> Optional[ConnectedAgent]:
        self.decommissioned_agents.discard(agent_id)

        # Permanent Lock: Preserve existing tag if previously registered or active for this agent_id
        if agent_id in self.registered_agents:
            existing_tag = self.registered_agents[agent_id].get("endpoint_tag")
            if existing_tag:
                info["endpoint_tag"] = existing_tag
        elif agent_id in self.active_agents:
            existing_tag = self.active_agents[agent_id].client_info.get("endpoint_tag")
            if existing_tag:
                info["endpoint_tag"] = existing_tag

        now = time.time()
        if agent_id in self.active_agents:
            existing = self.active_agents[agent_id]
            # Protect healthy active connections from being killed by duplicate background processes
            if (now - existing.last_heartbeat) < 15.0:
                logger.info(f"Rejecting duplicate connection for agent '{agent_id}' because an active session is already connected and healthy.")
                return None
            else:
                logger.info(f"Agent '{agent_id}' stale connection expired. Closing previous WebSocket and registering new connection.")
                try:
                    import asyncio
                    asyncio.create_task(existing.ws.close(code=1000, reason="Stale connection replaced"))
                except Exception:
                    pass

        agent = ConnectedAgent(agent_id, ws, info)
        self.active_agents[agent_id] = agent
        self.registered_agents[agent_id] = agent.to_dict()
        logger.info(f"Agent '{agent_id}' ({info.get('hostname')}) registered online with locked tag '{info.get('endpoint_tag')}'.")
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
            logger.info(f"Decommissioned agent '{agent_id}' removed on disconnect.")
            return
        if agent_id in self.registered_agents:
            self.registered_agents[agent_id]["status"] = "offline"
            self.registered_agents[agent_id]["last_heartbeat"] = time.time()
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
        return None

    def list_all_agents(self) -> list:
        # Returns list of all agents (online and offline, excluding decommissioned)
        results = []
        for aid, data in list(self.registered_agents.items()):
            if aid in self.decommissioned_agents:
                continue
            if aid in self.active_agents:
                results.append(self.active_agents[aid].to_dict())
            else:
                data["status"] = "offline"
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
            logger.info(f"Agent '{agent_id}' terminated and removed by administrator.")
            return True
        return False
