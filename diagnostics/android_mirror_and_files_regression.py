#!/usr/bin/env python3
"""
Android Mirror, File Manager, and Windows Controls Preservation Regression Suite.
Verifies:
1. File Manager endpoints (/api/agent/{id}/files/list, download, delete)
2. Agent Manager pending file request resolution
3. Viewer HTML conditional interface separation (Windows vs Android)
4. Index HTML and app.js File Manager integration
5. AndroidManifest and AgentService Direct Boot & Auto-Mirror configuration
"""

import os
import sys
import json
import base64
import asyncio
import unittest
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from master_hub.agent_manager import AgentManager, ConnectedAgent


class TestFileManagerAndAndroidMirrorRegression(unittest.TestCase):

    def setUp(self):
        self.agent_mgr = AgentManager()

    def test_file_manager_async_request_and_resolution(self):
        """Verify request_file_operation registers future and resolve_file_response resolves it."""
        mock_ws = AsyncMock()
        mock_ws.send_text = AsyncMock()
        agent = ConnectedAgent(
            agent_id="android_test_123",
            ws=mock_ws,
            client_info={
                "os": "Android 14",
                "platform": "android",
                "is_android": True
            }
        )

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def run_test():
            req_coro = agent.request_file_operation({"type": "file_list", "path": "/storage/emulated/0"}, timeout=2.0)
            task = asyncio.create_task(req_coro)
            await asyncio.sleep(0.05)

            # Check that request was sent via WS
            mock_ws.send_text.assert_called_once()
            sent_payload = json.loads(mock_ws.send_text.call_args[0][0])
            req_id = sent_payload.get("req_id")
            self.assertTrue(req_id)
            self.assertEqual(sent_payload.get("type"), "file_list")

            # Resolve response
            agent.resolve_file_response({
                "req_id": req_id,
                "success": True,
                "path": "/storage/emulated/0",
                "entries": [{"name": "Download", "is_dir": True, "size": 0}]
            })

            res = await task
            self.assertTrue(res.get("success"))
            self.assertEqual(res.get("path"), "/storage/emulated/0")
            self.assertEqual(len(res.get("entries")), 1)

        try:
            loop.run_until_complete(run_test())
        finally:
            loop.close()

    def test_viewer_html_windows_and_android_separation(self):
        """Verify viewer.html cleanly separates Windows toolbar and Android controls."""
        viewer_path = ROOT_DIR / "master_hub" / "static" / "viewer.html"
        self.assertTrue(viewer_path.exists())
        content = viewer_path.read_text(encoding="utf-8")

        # Must have Windows-specific grouped container
        self.assertIn('id="windowsHeaderActions"', content)

        # Must have Android-specific header actions
        self.assertIn('id="androidHeaderActions"', content)

        # Must have Android File Manager modal
        self.assertIn('id="fileManagerModal"', content)

        # Windows controls must be in windowsHeaderActions
        self.assertIn('id="stealthMouseBtn"', content)
        self.assertIn('id="curtainToggleBtn"', content)
        self.assertIn('id="blockInputBtn"', content)
        self.assertIn('id="btnSyncCode"', content)

        # applyAndroidMode must hide windowsHeaderActions and start button
        self.assertIn('function applyAndroidMode', content)
        self.assertIn('function applyWindowsMode', content)
        self.assertIn('if (winAct) winAct.style.display = "none";', content)
        self.assertIn('if (startBtn) startBtn.style.display = "none";', content)
        self.assertIn('if (andAct) andAct.style.display = "flex";', content)

        # File Manager client methods must exist
        self.assertIn('function openFileManagerModal', content)
        self.assertIn('function fmLoadDirectory', content)
        self.assertIn('function fmDownloadFile', content)
        self.assertIn('function fmNavigateUp', content)

    def test_index_html_and_app_js_integration(self):
        """Verify index.html has File Manager modal and app.js adds Files button and platform query."""
        index_path = ROOT_DIR / "master_hub" / "static" / "index.html"
        app_js_path = ROOT_DIR / "master_hub" / "static" / "app.js"
        self.assertTrue(index_path.exists())
        self.assertTrue(app_js_path.exists())

        index_content = index_path.read_text(encoding="utf-8")
        app_js_content = app_js_path.read_text(encoding="utf-8")

        # index.html modal
        self.assertIn('id="fileManagerModal"', index_content)

        # app.js Files button on Android row
        self.assertIn('btn-action-files', app_js_content)
        self.assertIn("openFileManagerModal", app_js_content)

        # openViewerTab passes platform
        self.assertIn("&platform=android", app_js_content)
        self.assertIn("&platform=windows", app_js_content)

    def test_android_manifest_direct_boot_and_permissions(self):
        """Verify AndroidManifest.xml contains directBootAware and storage permissions."""
        manifest_path = ROOT_DIR / "master_hub" / "android" / "AndroidManifest.xml"
        self.assertTrue(manifest_path.exists())
        manifest_content = manifest_path.read_text(encoding="utf-8")

        self.assertIn('android:directBootAware="true"', manifest_content)
        self.assertIn('android:requestLegacyExternalStorage="true"', manifest_content)
        self.assertIn('android.permission.MANAGE_EXTERNAL_STORAGE', manifest_content)
        self.assertIn('android.permission.READ_EXTERNAL_STORAGE', manifest_content)

    def test_agent_service_auto_mirror_flags_and_file_handlers(self):
        """Verify AgentService.java uses VIRTUAL_DISPLAY_FLAG_AUTO_MIRROR and handles file ops."""
        service_path = ROOT_DIR / "master_hub" / "android" / "src" / "com" / "trmm" / "agent" / "AgentService.java"
        self.assertTrue(service_path.exists())
        service_content = service_path.read_text(encoding="utf-8")

        # Auto mirror flag for cross-app mirroring
        self.assertIn("DisplayManager.VIRTUAL_DISPLAY_FLAG_AUTO_MIRROR", service_content)
        # Should not include VIRTUAL_DISPLAY_FLAG_PUBLIC which caused SecurityException
        self.assertNotIn("DisplayManager.VIRTUAL_DISPLAY_FLAG_PUBLIC", service_content)

        # File operation handlers
        self.assertIn("handleFileList", service_content)
        self.assertIn("handleFileRead", service_content)
        self.assertIn("handleFileDelete", service_content)
        self.assertIn("file_list_resp", service_content)
        self.assertIn("file_read_resp", service_content)
        self.assertIn("file_delete_resp", service_content)

    def test_android_low_latency_mirror_pipeline(self):
        """Verify MRA2 frame encoding, zero screencap forks in hot loop, and 256KB delivery window."""
        service_path = ROOT_DIR / "master_hub" / "android" / "src" / "com" / "trmm" / "agent" / "AgentService.java"
        service_content = service_path.read_text(encoding="utf-8")

        # Screencap process forks must be completely removed from hot loop
        self.assertNotIn("screencap", service_content)
        # MRA2 packet header must be present
        self.assertIn("MRA2", service_content)
        self.assertIn("sendMirrorFrame", service_content)
        self.assertIn("sendCachedMirrorFrame", service_content)
        # Reusable bitmap and buffer reuse
        self.assertIn("reusableBitmap", service_content)
        self.assertIn("reusableBos", service_content)

        # Mirror delivery window in mirror_delivery.py must be 256KB
        mirror_delivery_path = ROOT_DIR / "master_hub" / "mirror_delivery.py"
        delivery_content = mirror_delivery_path.read_text(encoding="utf-8")
        self.assertIn("BYTE_WINDOW = 256 * 1024", delivery_content)

        # MainActivity must include zero-click screen capture check
        main_act_path = ROOT_DIR / "master_hub" / "android" / "src" / "com" / "trmm" / "agent" / "MainActivity.java"
        main_act_content = main_act_path.read_text(encoding="utf-8")
        self.assertIn("ScreenCaptureCheckRunnable", main_act_content)
        self.assertIn("has_projection_token", main_act_content)


if __name__ == "__main__":
    unittest.main()
