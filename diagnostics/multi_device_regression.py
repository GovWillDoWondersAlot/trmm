import unittest
import asyncio
from unittest.mock import MagicMock, AsyncMock
from master_hub.agent_manager import AgentManager, ConnectedAgent

class MultiDeviceRegressionTests(unittest.TestCase):
    def setUp(self):
        self.mgr = AgentManager()

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

if __name__ == "__main__":
    unittest.main()
