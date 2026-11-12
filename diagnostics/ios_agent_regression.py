"""
Regression tests for Apple iOS Agent integration into Tactical RMM.
Validates:
1. IosBuilder .mobileconfig generation and QR Code encoding
2. AgentGenerator pipeline for 'ios' architecture
3. iOS Web Agent endpoints (/ios, /static/ios/manifest.json, /static/ios/sw.js)
4. Apple Profile download with application/x-apple-as-config Content-Type
5. iOS File Upload, Listing, Download, and Deletion
6. Remote Device Lock & WebSocket Handshake
7. Zero regressions on Windows and Android builds
"""

import os
import sys
import unittest
import json
import asyncio
from fastapi.testclient import TestClient

# Ensure workspace root is in sys.path
WORKSPACE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if WORKSPACE_DIR not in sys.path:
    sys.path.insert(0, WORKSPACE_DIR)

from master_hub.ios_builder import IosBuilder
from master_hub.generator import AgentGenerator
from master_hub.auth import AuthManager
from master_hub.app import app, agent_mgr


class TestIosAgentSuite(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)
        cls.test_agent_id = "test-ios-agent-999"
        cls.test_tag = "Executive-iPhone-15"
        cls.test_server_url = "http://127.0.0.1:8000"
        # Authenticate test client session
        token = AuthManager.create_session("admin")
        cls.client.cookies.set("trmm_session", token)

    def test_01_ios_builder_mobileconfig(self):
        """Test IosBuilder generates valid Apple Configuration Profile XML."""
        xml_content = IosBuilder.generate_mobileconfig(
            agent_id=self.test_agent_id,
            endpoint_tag=self.test_tag,
            server_url=self.test_server_url
        )
        self.assertIn("<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\"", xml_content)
        self.assertIn("<key>PayloadType</key>", xml_content)
        self.assertIn("<string>com.apple.webClip.managed</string>", xml_content)
        self.assertIn(f"/ios?agent_id={self.test_agent_id}", xml_content)
        self.assertIn("<key>FullScreen</key>", xml_content)
        self.assertIn("<true/>", xml_content)
        self.assertIn(self.test_tag, xml_content)

        # Confirm strictly valid XML Plist without unescaped tokens
        import plistlib
        parsed = plistlib.loads(xml_content.encode("utf-8"))
        self.assertEqual(parsed["PayloadType"], "Configuration")
        self.assertEqual(parsed["PayloadContent"][0]["PayloadType"], "com.apple.webClip.managed")

    def test_02_ios_builder_qr_code(self):
        """Test QR code generation creates a valid base64 PNG data URL."""
        qr_url = IosBuilder.generate_qr_code_base64("https://rmm.example.com/ios?agent_id=xyz")
        self.assertTrue(qr_url.startswith("data:image/png;base64,"))
        self.assertGreater(len(qr_url), 100)

    def test_03_agent_generator_ios_package(self):
        """Test AgentGenerator generates complete iOS package bundle with QR and profile."""
        result = AgentGenerator.build_package({
            "endpoint_tag": self.test_tag,
            "arch": "ios",
            "server_url": self.test_server_url,
            "custom_name": "MyCEOiPhone"
        })
        self.assertTrue(result.get("success"), f"Build failed: {result}")
        self.assertEqual(result.get("arch"), "ios")
        self.assertIn("profile_filename", result)
        self.assertTrue(result["profile_filename"].endswith(".mobileconfig"))
        self.assertTrue(result.get("qr_data_url", "").startswith("data:image/png;base64,"))
        self.assertIn("/ios?agent_id=", result.get("enroll_url", ""))
        self.assertIn("/api/agents/download/", result.get("profile_download_url", ""))

        # Check file exists on disk
        profile_path = os.path.join(WORKSPACE_DIR, "generated_agents", result["profile_filename"])
        self.assertTrue(os.path.exists(profile_path), f"Profile not saved to disk: {profile_path}")

    def test_04_ios_endpoints_and_mime(self):
        """Test iOS web agent route and Apple Configuration Profile MIME type."""
        # Test /ios page
        res = self.client.get(f"/ios?agent_id={self.test_agent_id}&tag={self.test_tag}")
        self.assertEqual(res.status_code, 200)
        self.assertIn("Tactical RMM — iOS Mobile Agent", res.text)
        self.assertIn("iosLockCurtain", res.text)

        # Test manifest
        res_manifest = self.client.get("/static/ios/manifest.json")
        self.assertEqual(res_manifest.status_code, 200)
        manifest_data = res_manifest.json()
        self.assertEqual(manifest_data.get("display"), "standalone")

        # Test service worker
        res_sw = self.client.get("/static/ios/sw.js")
        self.assertEqual(res_sw.status_code, 200)
        self.assertIn("self.addEventListener", res_sw.text)

        # Test .mobileconfig MIME type
        # First generate a test profile
        builder_res = IosBuilder.build_ios_package(
            agent_id="mime-check-agent",
            endpoint_tag="MimePhone",
            server_url="http://127.0.0.1:8000"
        )
        profile_fn = builder_res["profile_filename"]
        res_dl = self.client.get(f"/api/agents/download/{profile_fn}")
        self.assertEqual(res_dl.status_code, 200)
        self.assertIn("application/x-apple-aspen-config", res_dl.headers.get("content-type"))
        self.assertIn("com.apple.webClip.managed", res_dl.text)

        # Test dedicated profile and kiosk routes
        res_prof = self.client.get(f"/api/agent/mime-check-agent/profile.mobileconfig?tag=MimePhone")
        self.assertEqual(res_prof.status_code, 200)
        self.assertIn("application/x-apple-aspen-config", res_prof.headers.get("content-type"))

        res_kiosk = self.client.get(f"/api/agent/mime-check-agent/kiosk.mobileconfig?tag=MimePhone")
        self.assertEqual(res_kiosk.status_code, 200)
        self.assertIn("application/x-apple-aspen-config", res_kiosk.headers.get("content-type"))

    def test_05_ios_file_upload_and_management(self):
        """Test iOS device file upload, listing, download, and delete via RMM File Manager API."""
        agent_id = "test-ios-files-agent"
        # 1. Upload a file simulating Safari file/camera picker
        file_content = b"Simulated iPhone Photo Data 2026"
        files = {
            "files": ("photo_vacation.jpg", file_content, "image/jpeg")
        }
        res_upload = self.client.post(f"/api/agent/{agent_id}/files/upload", files=files)
        self.assertEqual(res_upload.status_code, 200)
        upload_data = res_upload.json()
        self.assertTrue(upload_data.get("success"))
        self.assertEqual(upload_data.get("uploaded_count"), 1)

        # 2. List files via RMM file manager
        res_list = self.client.post(f"/api/agent/{agent_id}/files/list", json={"path": "/uploads"})
        self.assertEqual(res_list.status_code, 200)
        list_data = res_list.json()
        self.assertTrue(list_data.get("success"))
        file_entries = list_data.get("entries", [])
        self.assertTrue(any(e["name"] == "photo_vacation.jpg" for e in file_entries))
        self.assertIn("/uploads", list_data.get("shortcuts", []))

        # 3. Download file
        target_path = next(e["path"] for e in file_entries if e["name"] == "photo_vacation.jpg")
        res_dl = self.client.get(f"/api/agent/{agent_id}/files/download?path={target_path}")
        self.assertEqual(res_dl.status_code, 200)
        self.assertEqual(res_dl.content, file_content)

        # 4. Delete file
        res_del = self.client.post(f"/api/agent/{agent_id}/files/delete", json={"path": target_path})
        self.assertEqual(res_del.status_code, 200)
        del_data = res_del.json()
        self.assertTrue(del_data.get("success"))

        # Confirm deleted
        res_list2 = self.client.post(f"/api/agent/{agent_id}/files/list", json={"path": "/uploads"})
        self.assertFalse(any(e["name"] == "photo_vacation.jpg" for e in res_list2.json().get("entries", [])))

    def test_06_remote_device_lock_curtain(self):
        """Test remote device lock curtain armed/dispatched via Master Hub API."""
        agent_id = "test-ios-lock-agent"
        res_lock = self.client.post(f"/api/agent/{agent_id}/curtain", json={
            "enabled": True,
            "title": "DEVICE LOCKED FOR MAINTENANCE",
            "message": "IT security lockdown active.",
            "subtitle": "Contact 555-0199"
        })
        self.assertEqual(res_lock.status_code, 200)
        lock_data = res_lock.json()
        self.assertIn(lock_data.get("status"), ("curtain_queued", "curtain_sent"))

        # Verify curtain armed in memory
        self.assertIn(agent_id, agent_mgr.pending_curtains)

        # Test unlocking
        res_unlock = self.client.post(f"/api/agent/{agent_id}/curtain", json={"enabled": False})
        self.assertEqual(res_unlock.status_code, 200)
        self.assertNotIn(agent_id, agent_mgr.pending_curtains)

    def test_07_windows_and_android_non_regression(self):
        """Confirm Windows and Android package generation remain fully operational."""
        # Windows package check
        win_result = AgentGenerator.build_package({
            "endpoint_tag": "Test-Win-Box",
            "arch": "x64",
            "server_url": self.test_server_url
        })
        self.assertTrue(win_result.get("success"))
        self.assertEqual(win_result.get("arch"), "x64")
        self.assertTrue(win_result.get("zip_filename", "").endswith(".zip"))
        self.assertTrue(win_result.get("bootstrap_filename", "").endswith(".bat"))

        # Android package check
        android_result = AgentGenerator.build_package({
            "endpoint_tag": "Test-Android-Device",
            "arch": "android",
            "server_url": self.test_server_url
        })
        self.assertTrue(android_result.get("success"))
        self.assertEqual(android_result.get("arch"), "android")
        self.assertTrue(android_result.get("apk_filename", "").endswith(".apk"))


if __name__ == "__main__":
    unittest.main()
