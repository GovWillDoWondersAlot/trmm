import unittest
import asyncio
import json
import os
import tempfile
import shutil
from unittest.mock import MagicMock, AsyncMock

from master_hub.agent_manager import AgentManager, ConnectedAgent
from master_hub.ota_manager import OTAManager

class OtaPersistenceRegressionTests(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="trmm_ota_test_")
        self.endpoints_file = os.path.join(self.test_dir, "endpoints.json")
        self.mgr = AgentManager(endpoints_file=self.endpoints_file, sync_packages=False)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_persistence_of_offline_and_online_agents(self):
        # 1. Register an online Android agent
        ws_mock = MagicMock()
        ws_mock.client = MagicMock(host="192.168.1.50")
        ws_mock.close = AsyncMock()

        info = {
            "hostname": "Samsung-Galaxy-S21",
            "device_name": "Galaxy S21 5G",
            "is_android": True,
            "arch": "android",
            "os": "Android 13",
            "endpoint_tag": "FieldTech",
            "code_hash": "old_hash_123",
            "app_version": "1.0.0"
        }

        agent = self.mgr.register_connection("agent_android_01", ws_mock, info)
        self.assertIsNotNone(agent)
        self.assertEqual(agent.status, "online")

        # Verify disk file was created and contains the agent
        self.assertTrue(os.path.exists(self.endpoints_file))
        with open(self.endpoints_file, "r", encoding="utf-8") as f:
            persisted = json.load(f)
        self.assertIn("agent_android_01", persisted)
        self.assertEqual(persisted["agent_android_01"]["hostname"], "Samsung-Galaxy-S21")

        # 2. Simulate disconnect
        self.mgr.unregister_connection("agent_android_01")
        self.assertNotIn("agent_android_01", self.mgr.active_agents)

        # 3. Verify agent remains visible in list_all_agents() as offline
        all_agents = self.mgr.list_all_agents()
        matched = [a for a in all_agents if a["agent_id"] == "agent_android_01"]
        self.assertEqual(len(matched), 1)
        self.assertEqual(matched[0]["status"], "offline")
        self.assertEqual(matched[0]["hostname"], "Samsung-Galaxy-S21")

        # 4. Instantiate a brand new AgentManager pointing to the same file (simulating server reboot)
        new_mgr = AgentManager(endpoints_file=self.endpoints_file, sync_packages=False)
        restored_agents = new_mgr.list_all_agents()
        self.assertEqual(len(restored_agents), 1)
        self.assertEqual(restored_agents[0]["agent_id"], "agent_android_01")
        self.assertEqual(restored_agents[0]["status"], "offline")

    def test_queue_update_when_offline_and_dispatch_on_reconnect(self):
        # Register and disconnect
        ws_mock = MagicMock()
        ws_mock.client = MagicMock(host="192.168.1.50")
        ws_mock.close = AsyncMock()

        info = {
            "hostname": "Infinix-Hot-10",
            "is_android": True,
            "arch": "android",
            "code_hash": "outdated_hash"
        }
        self.mgr.register_connection("agent_infinix_02", ws_mock, info)
        self.mgr.unregister_connection("agent_infinix_02")

        # Agent is now offline. Queue an update payload.
        update_payload = {
            "type": "ota_update",
            "download_url": "/api/agents/update/agent_infinix_02.apk",
            "target_hash": "new_target_hash_999",
            "version": "2.0.0"
        }
        queued = self.mgr.queue_update("agent_infinix_02", update_payload)
        self.assertTrue(queued)

        # Device is not yet connected, so payload sits in queue
        self.assertIn("agent_infinix_02", self.mgr.pending_updates)

        # Now device reconnects via WebSocket
        ws_reconnect = MagicMock()
        ws_reconnect.client = MagicMock(host="192.168.1.50")
        ws_reconnect.send_text = AsyncMock()

        self.mgr.register_connection("agent_infinix_02", ws_reconnect, info)

        # Pending update should have been popped and dispatched
        self.assertNotIn("agent_infinix_02", self.mgr.pending_updates)

    def test_ota_manifest_and_code_hash(self):
        manifest = OTAManager.get_android_manifest()
        self.assertIn("version", manifest)
        self.assertIn("code_hash", manifest)
        self.assertIn("download_url", manifest)
        self.assertTrue(len(manifest["code_hash"]) > 0)
        self.assertNotEqual(manifest["code_hash"], "unknown")

    def test_delete_all_devices(self):
        ws_mock = MagicMock()
        ws_mock.client = MagicMock(host="192.168.1.50")
        ws_mock.close = AsyncMock()

        self.mgr.register_connection("agent_to_delete_01", ws_mock, {"hostname": "Device1", "is_android": True})
        self.mgr.register_connection("agent_to_delete_02", ws_mock, {"hostname": "Device2", "is_android": False})

        self.assertEqual(len(self.mgr.list_all_agents()), 2)
        count = self.mgr.remove_all_agents()
        self.assertEqual(count, 2)
        self.assertEqual(len(self.mgr.list_all_agents()), 0)
        self.assertEqual(len(self.mgr.active_agents), 0)
        self.assertEqual(len(self.mgr.registered_agents), 0)

if __name__ == "__main__":
    unittest.main()
