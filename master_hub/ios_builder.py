"""
Tactical RMM — Apple iOS Mobile Agent Builder.
Generates Apple Configuration Profiles (.mobileconfig) with WebClip payloads,
generates enrollment QR codes for instant iPhone camera scanning,
and packages 1-click iOS Web Agent deployment bundles.
"""

import os
import io
import json
import uuid
import base64
import logging
from typing import Dict, Any, Optional
from urllib.parse import urlparse

try:
    import qrcode
except ImportError:
    qrcode = None

logger = logging.getLogger("master_hub.ios_builder")

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_DIR = os.path.join(ROOT_DIR, "generated_agents")
STATIC_DIR = os.path.join(ROOT_DIR, "master_hub", "static")


class IosBuilder:
    """Builder for Apple iOS mobile agent profiles and QR codes."""

    @staticmethod
    def generate_mobileconfig(config: Optional[Dict[str, Any]] = None, **kwargs) -> str:
        """
        Generates an Apple Configuration Profile XML (.mobileconfig)
        embedding a managed WebClip that pins the agent onto the iOS home screen.
        """
        cfg = {**(config or {}), **kwargs}
        agent_id = cfg.get("agent_id") or uuid.uuid4().hex[:8]
        endpoint_tag = cfg.get("endpoint_tag") or "iPhone"
        custom_name = (cfg.get("custom_name") or "").strip()
        app_name = custom_name if custom_name else endpoint_tag
        server_url = cfg.get("server_url") or "https://127.0.0.1:8000"
        enroll_url = cfg.get("enroll_url") or f"{server_url}/ios?agent_id={agent_id}&tag={endpoint_tag}"

        root_uuid = str(uuid.uuid4()).upper()
        webclip_uuid = str(uuid.uuid4()).upper()

        icon_xml = ""
        icon_b64 = cfg.get("icon_base64")
        if icon_b64:
            clean_b64 = icon_b64
            if "," in clean_b64:
                clean_b64 = clean_b64.split(",", 1)[1]
            clean_b64 = clean_b64.strip().replace("\n", "").replace("\r", "")
            icon_xml = f"""
            <key>Icon</key>
            <data>{clean_b64}</data>"""

        profile_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>PayloadContent</key>
    <array>
        <dict>
            <key>FullScreen</key>
            <true/>
            <key>IsRemovable</key>
            <true/>
            <key>Label</key>
            <string>{app_name}</string>
            <key>PayloadDescription</key>
            <string>Tactical RMM iOS Web Agent</string>
            <key>PayloadDisplayName</key>
            <string>{app_name}</string>
            <key>PayloadIdentifier</key>
            <string>com.trmm.agent.{agent_id}.webclip</string>
            <key>PayloadType</key>
            <string>com.apple.webClip.managed</string>
            <key>PayloadUUID</key>
            <string>{webclip_uuid}</string>
            <key>PayloadVersion</key>
            <integer>1</integer>
            <key>Precomposed</key>
            <true/>
            <key>URL</key>
            <string>{enroll_url}</string>{icon_xml}
        </dict>
    </array>
    <key>PayloadDescription</key>
    <string>Configures Tactical RMM Web Agent for iPhone and iPad</string>
    <key>PayloadDisplayName</key>
    <string>{app_name} Enrollment</string>
    <key>PayloadIdentifier</key>
    <string>com.trmm.agent.{agent_id}</string>
    <key>PayloadOrganization</key>
    <string>Tactical RMM</string>
    <key>PayloadRemovalDisallowed</key>
    <false/>
    <key>PayloadType</key>
    <string>Configuration</string>
    <key>PayloadUUID</key>
    <string>{root_uuid}</string>
    <key>PayloadVersion</key>
    <integer>1</integer>
</dict>
</plist>"""
        return profile_xml

    @staticmethod
    def generate_qr_code_base64(url: str) -> str:
        """Generates a high-contrast base64 PNG data URL of a QR code."""
        if not qrcode:
            logger.warning("qrcode package not installed. Skipping QR code generation.")
            return ""
        try:
            qr = qrcode.QRCode(
                version=1,
                error_correction=qrcode.constants.ERROR_CORRECT_M,
                box_size=8,
                border=2,
            )
            qr.add_data(url)
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white")
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            b64_str = base64.b64encode(buf.getvalue()).decode("utf-8")
            return f"data:image/png;base64,{b64_str}"
        except Exception as e:
            logger.warning(f"Error generating QR code: {e}")
            return ""

    @staticmethod
    def build_ios_package(config: Optional[Dict[str, Any]] = None, agent_id: Optional[str] = None,
                          endpoint_tag: Optional[str] = None, server_url: Optional[str] = None,
                          http_server_url: Optional[str] = None, **kwargs) -> Dict[str, Any]:
        """
        Builds the iOS agent package:
        - Standalone Apple Configuration Profile (.mobileconfig)
        - 1-Click Enrollment URL
        - QR code data URL for camera scanning
        """
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        cfg = {**(config or {}), **kwargs}
        agent_id = agent_id or cfg.get("agent_id") or uuid.uuid4().hex[:8]
        endpoint_tag = endpoint_tag or cfg.get("endpoint_tag") or "iPhone"
        server_url = server_url or cfg.get("server_url") or "ws://127.0.0.1:8000"
        if not http_server_url:
            http_server_url = cfg.get("http_server_url") or server_url.replace("wss://", "https://").replace("ws://", "http://")

        custom_name = (cfg.get("custom_name") or "").strip()
        app_name = custom_name if custom_name else endpoint_tag

        enroll_url = f"{http_server_url}/ios?agent_id={agent_id}&tag={endpoint_tag}"

        profile_cfg = {
            "agent_id": agent_id,
            "endpoint_tag": endpoint_tag,
            "custom_name": app_name,
            "server_url": server_url,
            "enroll_url": enroll_url,
            "icon_base64": cfg.get("icon_base64")
        }
        profile_xml = IosBuilder.generate_mobileconfig(profile_cfg)

        profile_filename = f"{app_name}_{endpoint_tag}_ios.mobileconfig"
        profile_path = os.path.join(OUTPUT_DIR, profile_filename)
        with open(profile_path, "w", encoding="utf-8", newline="\n") as pf:
            pf.write(profile_xml)

        profile_download_url = f"/api/agents/download/{profile_filename}"
        qr_code_data = IosBuilder.generate_qr_code_base64(enroll_url)

        logger.info(f"Generated Apple iOS Profile: {profile_path} ({len(profile_xml)} bytes)")

        return {
            "success": True,
            "arch": "ios",
            "installer_ready": True,
            "installer_error": None,
            "profile_path": profile_path,
            "profile_filename": profile_filename,
            "profile_download_url": profile_download_url,
            "enroll_url": enroll_url,
            "qr_data_url": qr_code_data,
            "app_name": app_name
        }
