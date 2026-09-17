"""
Tactical RMM — Agent Dynamic Provisioner & OTA Module Sync Engine.
Handles downloading code bundles, silent software/service installation, and hot reloading.
"""

import os
import sys
import json
import time
import zipfile
import urllib.request
import subprocess
import logging
from typing import Dict, Any, Tuple, Optional

logger = logging.getLogger("agent.provisioner")

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

def get_silent_startupinfo():
    if sys.platform == "win32":
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0  # SW_HIDE
        return si
    return None

class AgentProvisioner:
    """Manages OTA updates and silent endpoint provisioning."""

    @staticmethod
    def get_updates_dir(agent_id: str = None) -> str:
        """Returns standard writable path for dynamic updates, isolated per agent_id."""
        app_data = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or os.path.expanduser("~")
        folder_name = f"TRMM_Agent_{agent_id}" if agent_id else "TRMM_Agent"
        up_dir = os.path.join(app_data, folder_name, "updates")
        os.makedirs(up_dir, exist_ok=True)
        return up_dir

    @staticmethod
    def get_local_code_hash() -> str:
        """Reads local manifest hash if present."""
        updates_dir = AgentProvisioner.get_updates_dir()
        manifest_file = os.path.join(updates_dir, "manifest.json")
        if os.path.isfile(manifest_file):
            try:
                with open(manifest_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return data.get("overall_hash", "")
            except Exception:
                pass
        return "frozen_initial"

    @staticmethod
    def sync_code_bundle(server_url: str) -> Tuple[bool, str]:
        """Downloads latest code bundle from Master Hub and extracts to updates dir."""
        clean_url = server_url.rstrip("/").replace("ws://", "http://").replace("wss://", "https://")
        endpoint = f"{clean_url}/api/agent/code_bundle"
        updates_dir = AgentProvisioner.get_updates_dir()

        logger.info(f"Fetching code bundle from {endpoint}...")
        try:
            req = urllib.request.Request(endpoint, headers={"User-Agent": "TRMM-Agent-Provisioner/2.2"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                if resp.status != 200:
                    return False, f"Server returned HTTP {resp.status}"
                bundle_bytes = resp.read()

            temp_zip = os.path.join(updates_dir, "bundle_download.zip")
            with open(temp_zip, "wb") as f:
                f.write(bundle_bytes)

            with zipfile.ZipFile(temp_zip, "r") as zf:
                zf.extractall(updates_dir)

            try:
                os.remove(temp_zip)
            except Exception:
                pass

            # Prepend updates directory to sys.path so new files take precedence immediately
            if updates_dir not in sys.path:
                sys.path.insert(0, updates_dir)

            # Check for and run any post-update setup specifications
            AgentProvisioner.process_setup_recipe(updates_dir, clean_url)

            logger.info("Code bundle synced and extracted successfully.")
            return True, "Code bundle updated successfully"
        except Exception as e:
            logger.error(f"Failed to sync code bundle: {e}")
            return False, str(e)

    @staticmethod
    def process_setup_recipe(updates_dir: str, server_url: str):
        """Processes optional setup.json if shipped in update bundle."""
        setup_file = os.path.join(updates_dir, "setup.json")
        if not os.path.isfile(setup_file):
            return

        try:
            with open(setup_file, "r", encoding="utf-8") as f:
                recipe = json.load(f)

            # 1. Services to register/start
            for svc in recipe.get("require_services", []):
                name = svc.get("name")
                disp = svc.get("display_name", name)
                bin_p = svc.get("bin_path", "")
                st = svc.get("start_type", "auto")
                if name and bin_p:
                    AgentProvisioner.install_service(name, disp, bin_p, st)

            # 2. Packages to download and install silently
            for pkg in recipe.get("require_packages", []):
                down_url = pkg.get("download_url")
                if down_url:
                    if down_url.startswith("/"):
                        down_url = f"{server_url}{down_url}"
                    flags = pkg.get("silent_flags")
                    AgentProvisioner.install_package(down_url, silent_args=flags)

            # 3. Post-update python script
            post_script = os.path.join(updates_dir, "post_update.py")
            if os.path.isfile(post_script):
                try:
                    logger.info(f"Executing {post_script} in runtime...")
                    scope = {"__file__": post_script, "__name__": "__main__"}
                    with open(post_script, "r", encoding="utf-8") as ps_f:
                        code = compile(ps_f.read(), post_script, "exec")
                        exec(code, scope)
                    logger.info("post_update.py executed successfully.")
                except Exception as ex:
                    logger.warning(f"Error running post_update.py: {ex}")
        except Exception as e:
            logger.error(f"Error processing setup.json: {e}")

    @staticmethod
    def install_service(service_name: str, display_name: str, bin_path: str, start_type: str = "auto") -> Tuple[bool, str]:
        """Creates and starts a Windows Service silently."""
        logger.info(f"Provisioning service '{service_name}' ({bin_path})")
        try:
            # Check if service already exists
            chk = subprocess.run(["sc.exe", "query", service_name], capture_output=True, text=True, startupinfo=get_silent_startupinfo(), creationflags=CREATE_NO_WINDOW)
            if "does not exist" in chk.stderr.lower() or "does not exist" in chk.stdout.lower():
                # Create service
                cmd = [
                    "sc.exe", "create", service_name,
                    f"binPath= {bin_path}",
                    f"DisplayName= {display_name}",
                    f"start= {start_type}"
                ]
                res = subprocess.run(cmd, capture_output=True, text=True, startupinfo=get_silent_startupinfo(), creationflags=CREATE_NO_WINDOW)
                if res.returncode != 0:
                    return False, f"sc create failed: {res.stderr or res.stdout}"
            
            # Start service
            subprocess.run(["sc.exe", "start", service_name], capture_output=True, text=True, startupinfo=get_silent_startupinfo(), creationflags=CREATE_NO_WINDOW)
            return True, f"Service '{service_name}' configured and started."
        except Exception as e:
            return False, f"Service provisioning exception: {e}"

    @staticmethod
    def install_package(package_url: str, silent_args: Optional[str] = None, timeout: int = 300) -> Tuple[bool, str]:
        """Downloads an installer (.msi or .exe) and executes it completely silently in background."""
        logger.info(f"Provisioning package from {package_url}")
        try:
            filename = os.path.basename(package_url.split("?")[0]) or "package_installer.exe"
            temp_dir = os.environ.get("TEMP") or "C:\\Temp"
            dest_file = os.path.join(temp_dir, filename)

            # Download
            req = urllib.request.Request(package_url, headers={"User-Agent": "TRMM-Agent-Provisioner/2.2"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                with open(dest_file, "wb") as out_f:
                    out_f.write(resp.read())

            ext = os.path.splitext(filename)[1].lower()
            if ext == ".msi":
                # Standard msiexec silent install
                args = silent_args or "/qn /norestart"
                cmd = f'msiexec.exe /i "{dest_file}" {args}'
            else:
                # Default generic silent installer flags if not specified
                args = silent_args or "/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /SP-"
                cmd = f'"{dest_file}" {args}'

            logger.info(f"Executing silent installation: {cmd}")
            proc = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout, startupinfo=get_silent_startupinfo(), creationflags=CREATE_NO_WINDOW)
            
            # Remove installer
            try:
                os.remove(dest_file)
            except Exception:
                pass

            if proc.returncode == 0 or proc.returncode == 3010: # 3010 is ERROR_SUCCESS_REBOOT_REQUIRED
                return True, f"Installation successful (Exit code: {proc.returncode})"
            else:
                return False, f"Installer failed with exit code {proc.returncode}: {proc.stderr or proc.stdout}"
        except Exception as e:
            return False, f"Package installation failed: {e}"
