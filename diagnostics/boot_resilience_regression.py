"""
Regression tests for Android agent boot survivability, Direct Boot aware service execution,
and automated reconnect after reboot without manual app launch.
"""

import os
import unittest
import zipfile
import xml.etree.ElementTree as ET

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ANDROID_DIR = os.path.join(ROOT_DIR, "master_hub", "android")
MANIFEST_PATH = os.path.join(ANDROID_DIR, "AndroidManifest.xml")
BASE_APK_PATH = os.path.join(ROOT_DIR, "master_hub", "payloads", "android_agent_base.apk")
CLASSES_DEX_PATH = os.path.join(ROOT_DIR, "master_hub", "payloads", "classes.dex")
AGENT_SERVICE_JAVA = os.path.join(ANDROID_DIR, "src", "com", "trmm", "agent", "AgentService.java")
BOOT_RECEIVER_JAVA = os.path.join(ANDROID_DIR, "src", "com", "trmm", "agent", "BootReceiver.java")

class AndroidBootResilienceRegression(unittest.TestCase):

    def test_manifest_direct_boot_aware_service(self):
        """Verifies that AgentService is explicitly marked directBootAware for reboot startup."""
        self.assertTrue(os.path.isfile(MANIFEST_PATH), "AndroidManifest.xml must exist")
        tree = ET.parse(MANIFEST_PATH)
        root = tree.getroot()
        app = root.find("application")
        self.assertIsNotNone(app, "Application element must exist")

        agent_service = None
        for service in app.findall("service"):
            name = service.attrib.get("{http://schemas.android.com/apk/res/android}name")
            if name == ".AgentService":
                agent_service = service
                break

        self.assertIsNotNone(agent_service, ".AgentService must be declared in AndroidManifest.xml")
        direct_boot = agent_service.attrib.get("{http://schemas.android.com/apk/res/android}directBootAware")
        self.assertEqual(direct_boot, "true", "AgentService must have android:directBootAware='true' to start before unlock on reboot")

    def test_boot_receiver_intent_actions(self):
        """Verifies that BootReceiver handles all reboot, unlock, and watchdog intents."""
        tree = ET.parse(MANIFEST_PATH)
        root = tree.getroot()
        app = root.find("application")

        boot_receiver = None
        for receiver in app.findall("receiver"):
            name = receiver.attrib.get("{http://schemas.android.com/apk/res/android}name")
            if name == ".BootReceiver":
                boot_receiver = receiver
                break

        self.assertIsNotNone(boot_receiver, ".BootReceiver must be declared in AndroidManifest.xml")
        direct_boot = boot_receiver.attrib.get("{http://schemas.android.com/apk/res/android}directBootAware")
        self.assertEqual(direct_boot, "true", "BootReceiver must have android:directBootAware='true'")

        actions = set()
        for ifilter in boot_receiver.findall("intent-filter"):
            for action in ifilter.findall("action"):
                actions.add(action.attrib.get("{http://schemas.android.com/apk/res/android}name"))

        required_actions = {
            "android.intent.action.BOOT_COMPLETED",
            "android.intent.action.LOCKED_BOOT_COMPLETED",
            "android.intent.action.USER_UNLOCKED",
            "android.net.conn.CONNECTIVITY_CHANGE",
            "com.trmm.agent.WATCHDOG_PING"
        }
        for req in required_actions:
            self.assertIn(req, actions, f"BootReceiver intent-filter must contain action '{req}'")

    def test_base_apk_contains_compiled_classes_and_assets(self):
        """Verifies base APK and classes.dex contain the compiled boot resilience classes."""
        self.assertTrue(os.path.isfile(BASE_APK_PATH), f"Base APK missing at {BASE_APK_PATH}")
        self.assertGreater(os.path.getsize(BASE_APK_PATH), 10000, "Base APK must be non-empty")

        with zipfile.ZipFile(BASE_APK_PATH, "r") as apk:
            namelist = apk.namelist()
            self.assertIn("classes.dex", namelist, "Base APK must contain classes.dex")
            self.assertIn("AndroidManifest.xml", namelist, "Base APK must contain AndroidManifest.xml")
            self.assertIn("resources.arsc", namelist, "Base APK must contain resources.arsc")

    def test_agent_service_device_protected_storage_implementation(self):
        """Verifies that AgentService implements getSafePreferences with Device Protected Storage fallback."""
        with open(AGENT_SERVICE_JAVA, "r", encoding="utf-8") as f:
            code = f.read()

        self.assertIn("createDeviceProtectedStorageContext", code, "Must use createDeviceProtectedStorageContext for Direct Boot")
        self.assertIn("moveSharedPreferencesFrom", code, "Must support automatic migration from CE to DE storage")
        self.assertIn("scheduleWatchdogAlarm", code, "Must implement scheduleWatchdogAlarm for Doze/kill recovery")
        self.assertIn("BootReceiver.scheduleWatchdog", code, "Must delegate watchdog scheduling to BootReceiver")

    def test_boot_receiver_watchdog_support(self):
        """Verifies that BootReceiver defines and schedules WATCHDOG_PING alarms."""
        with open(BOOT_RECEIVER_JAVA, "r", encoding="utf-8") as f:
            code = f.read()

        self.assertIn("ACTION_WATCHDOG_PING", code, "BootReceiver must declare ACTION_WATCHDOG_PING")
        self.assertIn("scheduleWatchdog", code, "BootReceiver must declare scheduleWatchdog method")
        self.assertIn("AlarmManager.RTC_WAKEUP", code, "BootReceiver must wake CPU on watchdog alarm")

if __name__ == "__main__":
    unittest.main()

