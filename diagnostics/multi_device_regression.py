import unittest
import asyncio
from unittest.mock import MagicMock, AsyncMock
from master_hub.agent_manager import AgentManager, ConnectedAgent

class MultiDeviceRegressionTests(unittest.TestCase):
    def setUp(self):
        self.mgr = AgentManager(endpoints_file="")

    def test_multi_android_disambiguation(self):
        # Mock WebSocket for Phone 1 (Samsung)
        ws_samsung = MagicMock()
        ws_samsung.client = MagicMock(host="192.168.18.27")
        ws_samsung.close = AsyncMock()

        info_samsung = {
            "hostname": "samsung SM-G975F",
            "device_name": "samsung SM-G975F",
            "local_ip": "192.168.18.27",
            "is_android": True,
            "arch": "android",
            "os": "Android device",
            "endpoint_tag": "Zoom"
        }

        # Mock WebSocket for Phone 2 (Infinix)
        ws_infinix = MagicMock()
        ws_infinix.client = MagicMock(host="192.168.18.9")
        ws_infinix.close = AsyncMock()

        info_infinix = {
            "hostname": "INFINIX MOBILITY LIMITED Infinix X657C",
            "device_name": "INFINIX MOBILITY LIMITED Infinix X657C",
            "local_ip": "192.168.18.9",
            "is_android": True,
            "arch": "android",
            "os": "Android device",
            "endpoint_tag": "Zoom"
        }

        # Both connect using the identical package ID '60089aea'
        agent_samsung = self.mgr.register_connection("60089aea", ws_samsung, info_samsung)
        self.assertIsNotNone(agent_samsung)
        samsung_id = agent_samsung.agent_id

        agent_infinix = self.mgr.register_connection("60089aea", ws_infinix, info_infinix)
        self.assertIsNotNone(agent_infinix)
        infinix_id = agent_infinix.agent_id

        # 1. Distinct IDs assigned
        self.assertNotEqual(samsung_id, infinix_id)

        # 2. Samsung socket was NOT closed by Infinix registration
        ws_samsung.close.assert_not_called()

        # 3. Both devices active simultaneously in active_agents
        self.assertIn(samsung_id, self.mgr.active_agents)
        self.assertIn(infinix_id, self.mgr.active_agents)
        self.assertEqual(len(self.mgr.active_agents), 2)

        # 4. list_all_agents lists both devices online simultaneously
        all_agents = self.mgr.list_all_agents()
        online_agents = [a for a in all_agents if a.get("status") == "online"]
        self.assertEqual(len(online_agents), 2)
        agent_hosts = {a.get("hostname") for a in online_agents}
        self.assertIn("samsung SM-G975F", agent_hosts)
        self.assertIn("INFINIX MOBILITY LIMITED Infinix X657C", agent_hosts)

        # 5. Device 1 reconnects: should supersede only Device 1's socket
        ws_samsung_reconnect = MagicMock()
        ws_samsung_reconnect.client = MagicMock(host="192.168.18.27")
        ws_samsung_reconnect.close = AsyncMock()

        info_samsung_reconnect = dict(info_samsung)
        agent_samsung_2 = self.mgr.register_connection("60089aea", ws_samsung_reconnect, info_samsung_reconnect)
        self.assertEqual(agent_samsung_2.agent_id, samsung_id)

        # Old Samsung socket was superseded
        ws_samsung.close.assert_called_once()
        # Infinix socket was NEVER touched
        ws_infinix.close.assert_not_called()

        # Both devices still online
        self.assertEqual(len(self.mgr.active_agents), 2)
        self.assertIn(infinix_id, self.mgr.active_agents)
        self.assertIn(samsung_id, self.mgr.active_agents)

    def test_windows_and_android_coexistence(self):
        # Windows agent with unique machine GUID
        ws_win = MagicMock()
        ws_win.client = MagicMock(host="192.168.1.100")
        info_win = {
            "hostname": "DESKTOP-TEST",
            "os": "Windows 11",
            "arch": "x64",
            "is_android": False,
            "endpoint_tag": "Accounting"
        }
        agent_win = self.mgr.register_connection("win_guid_123", ws_win, info_win)
        self.assertEqual(agent_win.agent_id, "win_guid_123")

        # Android agent
        ws_android = MagicMock()
        ws_android.client = MagicMock(host="192.168.1.101")
        info_android = {
            "hostname": "Pixel 7",
            "os": "Android device",
            "arch": "android",
            "is_android": True,
            "endpoint_tag": "Mobile-1"
        }
        agent_android = self.mgr.register_connection("60089aea", ws_android, info_android)

        all_agents = self.mgr.list_all_agents()
        self.assertEqual(len(all_agents), 2)
        win_rec = [a for a in all_agents if a["agent_id"] == "win_guid_123"][0]
        self.assertEqual(win_rec["os"], "Windows 11")
        self.assertEqual(win_rec["platform"], "windows")

    def test_device_owner_telemetry_reporting(self):
        ws_android = MagicMock()
        ws_android.client = MagicMock(host="192.168.18.50")
        info_android = {
            "hostname": "Infinix-X657C",
            "device_name": "Infinix X657C",
            "local_ip": "192.168.18.50",
            "is_android": True,
            "arch": "android",
            "os": "Android device",
            "is_device_owner": True,
            "is_device_admin": True,
            "uninstall_blocked": True,
            "management_mode": "Device Owner (Corporate)"
        }
        agent = self.mgr.register_connection("dpm_agent_01", ws_android, info_android)
        agent_dict = agent.to_dict()
        self.assertTrue(agent_dict["is_device_owner"])
        self.assertTrue(agent_dict["is_device_admin"])
        self.assertTrue(agent_dict["uninstall_blocked"])
        self.assertEqual(agent_dict["management_mode"], "Device Owner (Corporate)")

        all_agents = self.mgr.list_all_agents()
        matched = [a for a in all_agents if a["agent_id"] == agent.agent_id][0]
        self.assertTrue(matched["is_device_owner"])
        self.assertTrue(matched["uninstall_blocked"])

    def test_device_owner_policy_dispatch(self):
        ws = MagicMock()
        ws.send_text = AsyncMock()
        ws.client = MagicMock(host="192.168.18.60")

        agent = self.mgr.register_connection("policy_agent_01", ws, {
            "hostname": "Samsung-Tablet",
            "is_android": True,
            "is_device_owner": True
        })

        import json
        asyncio.run(agent.send_text_safe(json.dumps({
            "type": "admin_policy",
            "action": "set_uninstall_blocked",
            "blocked": True
        })))

        ws.send_text.assert_called_once()
        sent_data = json.loads(ws.send_text.call_args[0][0])
        self.assertEqual(sent_data["type"], "admin_policy")
        self.assertEqual(sent_data["action"], "set_uninstall_blocked")
        self.assertTrue(sent_data["blocked"])

if __name__ == "__main__":
    unittest.main()

