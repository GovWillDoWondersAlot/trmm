"""
Regression test for Android Agent App Lifecycle, Play Protect Safety, and Lock Pinning Persistence.
Verifies:
1. MainActivity does not auto-minimize to background on launch/resume.
2. CurtainActivity protects against Samsung One UI "cannot pin" failure and reasserts lock.
3. AgentService executes active curtain watchdog to keep screen pinned and locked.
4. AndroidManifest.xml does not request prohibited/privileged system permissions.
5. Base APK and dynamically generated APKs are signed with production release certificate.
"""

import os
import sys
import unittest
import zipfile
import subprocess
import xml.etree.ElementTree as ET

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)
BASE_APK_PATH = os.path.join(ROOT_DIR, "master_hub", "payloads", "android_agent_base.apk")
MANIFEST_PATH = os.path.join(ROOT_DIR, "master_hub", "android", "AndroidManifest.xml")
MAIN_ACTIVITY_PATH = os.path.join(ROOT_DIR, "master_hub", "android", "src", "com", "trmm", "agent", "MainActivity.java")
CURTAIN_ACTIVITY_PATH = os.path.join(ROOT_DIR, "master_hub", "android", "src", "com", "trmm", "agent", "CurtainActivity.java")
AGENT_SERVICE_PATH = os.path.join(ROOT_DIR, "master_hub", "android", "src", "com", "trmm", "agent", "AgentService.java")


class TestAndroidLifecycleAndLockPinning(unittest.TestCase):

    def test_01_main_activity_no_auto_close_on_launch(self):
        """Verifies MainActivity does not auto-minimize with MoveToBackRunnable on user launch or resume."""
        with open(MAIN_ACTIVITY_PATH, "r", encoding="utf-8") as f:
            content = f.read()

        # In onCreate, auto move to back must be removed
        self.assertNotIn("handler.postDelayed(new MoveToBackRunnable(this), 3500L)", content,
                         "MainActivity must not automatically close after 3.5s in onCreate()")

        # In onResume, auto move to back when admin is active must be removed
        self.assertNotIn("handler.postDelayed(new MoveToBackRunnable(this), 500L)", content,
                         "MainActivity must not automatically close on onResume()")

        # Overlay permission prompt must be scheduled
        self.assertIn("OverlayCheckRunnable", content,
                      "MainActivity must schedule OverlayCheckRunnable to enable bulletproof system overlay")

    def test_02_curtain_activity_samsung_pinning_protection(self):
        """Verifies CurtainActivity protects against Samsung 'cannot pin' error and guards startLockTask."""
        with open(CURTAIN_ACTIVITY_PATH, "r", encoding="utf-8") as f:
            content = f.read()

        # Must verify permission before calling startLockTask
        self.assertIn("dpm.isDeviceOwnerApp(getPackageName())", content)
        self.assertIn("dpm.isLockTaskPermitted(getPackageName())", content)

        # Foreground state tracking must be present
        self.assertIn("public static volatile boolean isForeground", content)

        # Must close system dialogs on focus changes and home press
        self.assertIn("ACTION_CLOSE_SYSTEM_DIALOGS", content)

    def test_03_agent_service_curtain_watchdog(self):
        """Verifies AgentService includes active CurtainWatchdog to reassert lock if user attempts escape."""
        with open(AGENT_SERVICE_PATH, "r", encoding="utf-8") as f:
            content = f.read()

        self.assertIn("CurtainWatchdogRunnable", content,
                      "AgentService must define CurtainWatchdogRunnable")
        self.assertIn("performCurtainWatchdog", content,
                      "AgentService must implement performCurtainWatchdog")
        self.assertIn("startCurtainWatchdog", content,
                      "AgentService must start watchdog on lock activation")
        self.assertIn("stopCurtainWatchdog", content,
                      "AgentService must stop watchdog on unlock")

    def test_04_manifest_play_protect_permissions(self):
        """Verifies AndroidManifest does not contain privileged signature permissions flagged by Play Protect."""
        tree = ET.parse(MANIFEST_PATH)
        root = tree.getroot()

        permissions = set()
        for elem in root.findall("uses-permission"):
            name = elem.attrib.get("{http://schemas.android.com/apk/res/android}name")
            if name:
                permissions.add(name)

        # Prohibited permissions that trigger Google Play Protect malware / harmful app warnings
        self.assertNotIn("android.permission.INSTALL_PACKAGES", permissions,
                         "System-only INSTALL_PACKAGES permission must not be declared")
        self.assertNotIn("android.permission.FOREGROUND_SERVICE_SYSTEM_EXEMPTED", permissions,
                         "Restricted FOREGROUND_SERVICE_SYSTEM_EXEMPTED must not be declared")
        self.assertNotIn("android.permission.USE_EXACT_ALARM", permissions,
                         "USE_EXACT_ALARM is restricted; use SCHEDULE_EXACT_ALARM instead")

        # Required permissions for RMM operation
        self.assertIn("android.permission.REQUEST_INSTALL_PACKAGES", permissions)
        self.assertIn("android.permission.SYSTEM_ALERT_WINDOW", permissions)
        self.assertIn("android.permission.SCHEDULE_EXACT_ALARM", permissions)

    def test_05_base_apk_production_signature(self):
        """Verifies the base APK is signed with the production release certificate and v1+v2+v3 schemes."""
        self.assertTrue(os.path.isfile(BASE_APK_PATH), f"Base APK missing at {BASE_APK_PATH}")

        from master_hub.android_builder import APKSIGNER
        if APKSIGNER and os.path.isfile(APKSIGNER):
            res = subprocess.run([APKSIGNER, "verify", "--verbose", "--print-certs", BASE_APK_PATH],
                                 capture_output=True, text=True)
            self.assertEqual(res.returncode, 0, f"apksigner verify failed: {res.stderr}")
            self.assertIn("Verified using v1 scheme (JAR signing): true", res.stdout)
            self.assertIn("Verified using v2 scheme (APK Signature Scheme v2): true", res.stdout)
            self.assertIn("Verified using v3 scheme (APK Signature Scheme v3): true", res.stdout)
            self.assertIn("CN=Tactical Endpoint Management", res.stdout,
                          "APK must be signed with Production Release Certificate (not debug key)")
            self.assertNotIn("CN=Android Debug", res.stdout,
                             "APK must NOT be signed with Android Debug certificate")


if __name__ == "__main__":
    unittest.main()
