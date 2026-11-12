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
import plistlib
import logging
from typing import Dict, Any, Optional
from urllib.parse import urlparse

try:
    import qrcode
    try:
        import qrcode.image.svg
    except Exception:
        pass
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
        Uses plistlib to guarantee 100% valid, properly escaped Apple XML Plist syntax.
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
        applock_uuid = str(uuid.uuid4()).upper()

        is_kiosk = bool(cfg.get("kiosk_mode") or kwargs.get("kiosk_mode"))

        clean_pkg = "".join(c for c in app_name.lower() if c.isalnum() or c in ("-", "_")).strip() or "agent"

        # 1. Managed WebClip Payload (Pins the PWA Web Agent to iOS Home Screen)
        webclip_dict: Dict[str, Any] = {
            "PayloadType": "com.apple.webClip.managed",
            "PayloadVersion": 1,
            "PayloadIdentifier": f"com.{clean_pkg}.agent.{agent_id}.webclip",
            "PayloadUUID": webclip_uuid,
            "PayloadDisplayName": app_name,
            "PayloadDescription": f"{app_name} Web Agent",
            "Label": app_name,
            "URL": enroll_url,
            "FullScreen": True,
            "Precomposed": True,
            "IsRemovable": True,  # Allows installation on standard retail/unsupervised iPhones
        }

        icon_b64 = cfg.get("icon_base64")
        if icon_b64:
            clean_b64 = icon_b64
            if "," in clean_b64:
                clean_b64 = clean_b64.split(",", 1)[1]
            clean_b64 = clean_b64.strip().replace("\n", "").replace("\r", "")
            try:
                webclip_dict["Icon"] = base64.b64decode(clean_b64)
            except Exception as e_icon:
                logger.debug(f"Failed to decode icon bytes: {e_icon}")

        payloads = [webclip_dict]

        # 2. Hardware Kiosk Lockdown Payload (Single App Mode com.apple.app.lock)
        if is_kiosk:
            applock_dict: Dict[str, Any] = {
                "PayloadType": "com.apple.app.lock",
                "PayloadVersion": 1,
                "PayloadIdentifier": f"com.{clean_pkg}.agent.{agent_id}.applock",
                "PayloadUUID": applock_uuid,
                "PayloadDisplayName": f"{app_name} Hardware Kiosk Lockdown",
                "PayloadDescription": f"Locks the physical iOS device into {app_name} kiosk mode",
                "App": {
                    "Identifier": "com.apple.webapp",
                    "Options": {
                        "DisableAutoLock": True,
                        "DisableTouch": False,
                        "DisableDeviceRotation": False,
                        "DisableVolumeButtons": False,
                        "DisableRingerSwitch": False,
                        "DisableSleepWakeButton": False,
                        "EnableControlCenter": False,
                        "EnableNotificationCenter": False,
                    }
                }
            }
            payloads.append(applock_dict)

        display_name = f"{app_name} Kiosk Lockdown" if is_kiosk else app_name
        desc = f"Configures {app_name} with Hardware Kiosk Lockdown for iPhone and iPad" if is_kiosk else f"Configures {app_name} Web Agent for iPhone and iPad"

        profile_dict = {
            "PayloadType": "Configuration",
            "PayloadVersion": 1,
            "PayloadIdentifier": f"com.{clean_pkg}.agent.{agent_id}{'.kiosk' if is_kiosk else ''}",
            "PayloadUUID": root_uuid,
            "PayloadDisplayName": display_name,
            "PayloadDescription": desc,
            "PayloadOrganization": app_name,  # Clean custom organization name, no Tactical RMM branding!
            "PayloadRemovalDisallowed": False,  # False permits clean uninstallation from iOS Settings
            "PayloadContent": payloads
        }

        # Generate strictly valid Apple Property List XML
        profile_xml = plistlib.dumps(profile_dict, fmt=plistlib.FMT_XML).decode("utf-8")
        return profile_xml

    @staticmethod
    def sign_profile(profile_bytes: bytes, app_name: str = "Mobile Agent") -> bytes:
        """
        Cryptographically signs an Apple Configuration Profile (.mobileconfig) using PKCS#7 / CMS in DER format.
        Creates an Apple-compliant signed profile with the custom app_name publisher identity.
        """
        try:
            from cryptography import x509
            from cryptography.x509.oid import NameOID
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import rsa
            from cryptography.hazmat.primitives.serialization import pkcs7
            import datetime

            cert_dir = os.path.join(ROOT_DIR, "master_hub", "certs")
            os.makedirs(cert_dir, exist_ok=True)
            safe_name = "".join(c for c in app_name if c.isalnum() or c in ("-", "_")).strip() or "agent"
            key_path = os.path.join(cert_dir, f"{safe_name}_ios.key")
            crt_path = os.path.join(cert_dir, f"{safe_name}_ios.crt")

            if os.path.isfile(key_path) and os.path.isfile(crt_path):
                with open(key_path, "rb") as kf:
                    key = serialization.load_pem_private_key(kf.read(), password=None)
                with open(crt_path, "rb") as cf:
                    cert = x509.load_pem_x509_certificate(cf.read())
            else:
                key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
                subject = issuer = x509.Name([
                    x509.NameAttribute(NameOID.COUNTRY_NAME, "US"),
                    x509.NameAttribute(NameOID.ORGANIZATION_NAME, app_name),
                    x509.NameAttribute(NameOID.COMMON_NAME, f"{app_name} Publisher"),
                ])
                now = datetime.datetime.now(datetime.timezone.utc)
                cert = (
                    x509.CertificateBuilder()
                    .subject_name(subject)
                    .issuer_name(issuer)
                    .public_key(key.public_key())
                    .serial_number(x509.random_serial_number())
                    .not_valid_before(now - datetime.timedelta(days=1))
                    .not_valid_after(now + datetime.timedelta(days=3650))
                    .sign(key, hashes.SHA256())
                )
                with open(key_path, "wb") as kf:
                    kf.write(key.private_bytes(
                        encoding=serialization.Encoding.PEM,
                        format=serialization.PrivateFormat.TraditionalOpenSSL,
                        encryption_algorithm=serialization.NoEncryption()
                    ))
                with open(crt_path, "wb") as cf:
                    cf.write(cert.public_bytes(serialization.Encoding.PEM))

            builder = pkcs7.PKCS7SignatureBuilder().set_data(profile_bytes)
            builder = builder.add_signer(cert, key, hashes.SHA256())
            signed_der = builder.sign(serialization.Encoding.DER, [pkcs7.PKCS7Options.Binary])
            return signed_der
        except Exception as e:
            logger.warning(f"Failed to cryptographically sign iOS profile: {e}")
            return profile_bytes

    @staticmethod
    def generate_qr_code_base64(url: str) -> str:
        """Generates a high-contrast base64 PNG or SVG data URL of a QR code."""
        if qrcode:
            # 1. Try raster PNG with PIL
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
            except Exception as e_png:
                logger.debug(f"PNG QR generation failed ({e_png}), trying pure-SVG fallback...")

            # 2. Try pure SVG with SvgImage (Zero-dependency, no PIL required)
            try:
                if hasattr(qrcode, "image") and hasattr(qrcode.image, "svg"):
                    qr_svg = qrcode.QRCode(
                        version=1,
                        error_correction=qrcode.constants.ERROR_CORRECT_M,
                        box_size=8,
                        border=2,
                        image_factory=qrcode.image.svg.SvgImage
                    )
                    qr_svg.add_data(url)
                    qr_svg.make(fit=True)
                    svg_img = qr_svg.make_image()
                    buf = io.BytesIO()
                    svg_img.save(buf)
                    b64_svg = base64.b64encode(buf.getvalue()).decode("utf-8")
                    return f"data:image/svg+xml;base64,{b64_svg}"
            except Exception as e_svg:
                logger.warning(f"SVG QR generation failed: {e_svg}")

        # 3. Fallback: return standard HTTPS QR rendering API data URL
        from urllib.parse import quote
        return f"https://api.qrserver.com/v1/create-qr-code/?size=200x200&data={quote(url)}"

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
        # 1. Standard Enrollment WebClip Profile
        profile_xml = IosBuilder.generate_mobileconfig(profile_cfg)
        signed_profile = IosBuilder.sign_profile(profile_xml.encode("utf-8"), app_name)
        profile_filename = f"{app_name}_{endpoint_tag}_ios.mobileconfig"
        profile_path = os.path.join(OUTPUT_DIR, profile_filename)
        with open(profile_path, "wb") as pf:
            pf.write(signed_profile)

        # 2. Hardware Kiosk Lockdown Profile (Single App Mode com.apple.app.lock)
        kiosk_cfg = {**profile_cfg, "kiosk_mode": True}
        kiosk_xml = IosBuilder.generate_mobileconfig(kiosk_cfg)
        signed_kiosk = IosBuilder.sign_profile(kiosk_xml.encode("utf-8"), app_name)
        kiosk_filename = f"{app_name}_{endpoint_tag}_ios_kiosk.mobileconfig"
        kiosk_path = os.path.join(OUTPUT_DIR, kiosk_filename)
        with open(kiosk_path, "wb") as kf:
            kf.write(signed_kiosk)

        profile_download_url = f"/api/agents/download/{profile_filename}"
        kiosk_download_url = f"/api/agents/download/{kiosk_filename}"
        qr_code_data = IosBuilder.generate_qr_code_base64(enroll_url)

        logger.info(f"Generated Apple iOS Profiles: {profile_path} ({len(profile_xml)} bytes) and {kiosk_path} ({len(kiosk_xml)} bytes)")

        return {
            "success": True,
            "arch": "ios",
            "installer_ready": True,
            "installer_error": None,
            "profile_path": profile_path,
            "profile_filename": profile_filename,
            "profile_download_url": profile_download_url,
            "kiosk_path": kiosk_path,
            "kiosk_filename": kiosk_filename,
            "kiosk_download_url": kiosk_download_url,
            "enroll_url": enroll_url,
            "qr_data_url": qr_code_data,
            "app_name": app_name
        }
