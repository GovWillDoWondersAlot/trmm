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
SDK_DIR = os.path.expanduser(r"~\AppData\Local\Android\Sdk")
BUILD_TOOLS_DIR = os.path.join(SDK_DIR, "build-tools", "34.0.0")
if not os.path.isdir(BUILD_TOOLS_DIR):
    BUILD_TOOLS_DIR = os.path.join(SDK_DIR, "build-tools", "35.0.0")

APKSIGNER = os.path.join(BUILD_TOOLS_DIR, "apksigner.bat")
ZIPALIGN = os.path.join(BUILD_TOOLS_DIR, "zipalign.exe")
DEBUG_KEYSTORE = os.path.expanduser(r"~\.android\debug.keystore")


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
        Bakes custom config into the base APK template and signs it.
        """
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
            "auto_start": auto_start
        }

        temp_apk = output_apk_path + f".{uuid.uuid4().hex[:6]}.tmp.apk"
        try:
            # 1. Repack base APK without old signatures and with new assets/config.json
            with zipfile.ZipFile(BASE_APK_PATH, "r") as zin:
                with zipfile.ZipFile(temp_apk, "w", zipfile.ZIP_DEFLATED) as zout:
                    for item in zin.infolist():
                        if item.filename == "assets/config.json" or item.filename.startswith("META-INF/"):
                            continue
                        zout.writestr(item, zin.read(item.filename))
                    zout.writestr("assets/config.json", json.dumps(custom_cfg, indent=2))

            # 2. Resign APK using apksigner if available
            if os.path.isfile(APKSIGNER) and os.path.isfile(DEBUG_KEYSTORE):
                aligned_tmp = output_apk_path + f".{uuid.uuid4().hex[:6]}.aligned.apk"
                if os.path.isfile(ZIPALIGN):
                    subprocess.run([ZIPALIGN, "-f", "-p", "4", temp_apk, aligned_tmp],
                                   capture_output=True, check=True)
                    target_to_sign = aligned_tmp
                else:
                    target_to_sign = temp_apk

                sign_cmd = [
                    APKSIGNER, "sign",
                    "--ks", DEBUG_KEYSTORE,
                    "--ks-pass", "pass:android",
                    "--out", output_apk_path,
                    target_to_sign
                ]
                subprocess.run(sign_cmd, capture_output=True, check=True)
                if os.path.isfile(aligned_tmp):
                    os.remove(aligned_tmp)
            else:
                # If apksigner not present, use the updated APK directly
                os.replace(temp_apk, output_apk_path)

            if os.path.isfile(temp_apk):
                os.remove(temp_apk)
            return True
        except Exception as e:
            logger.error("Failed to build customized APK: %s", e)
            if os.path.isfile(temp_apk):
                os.remove(temp_apk)
            return False
