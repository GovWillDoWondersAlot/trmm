"""
Tactical RMM — Over-The-Air (OTA) Code & Payload Manager.
Generates code bundles, calculates manifests, and serves dependency payloads.
"""

import os
import io
import sys
import json
import zipfile
import hashlib
from typing import Dict, Any, Tuple, Optional

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAYLOADS_DIR = os.path.join(os.path.dirname(__file__), "payloads")
os.makedirs(PAYLOADS_DIR, exist_ok=True)

SYNC_DIRS = ["hvnc", "agent_client"]
ROOT_FILES = ["post_update.py", "setup.json", "agent_service.py"]
EXCLUDE_DIRS = {"__pycache__", ".git", ".pytest_cache", ".idea", ".vscode"}
EXCLUDE_EXTS = {".pyc", ".pyo", ".pyd"}

class OTAManager:
    """Manages live code bundling, change detection, and payload delivery."""

    @staticmethod
    def get_code_manifest() -> Dict[str, Any]:
        """Calculates SHA256 hashes of all source files in sync directories."""
        files_manifest = {}
        hasher = hashlib.sha256()

        for d in SYNC_DIRS:
            base_dir = os.path.join(ROOT_DIR, d)
            if not os.path.isdir(base_dir):
                continue
            for root, dirs, files in os.walk(base_dir):
                dirs[:] = [sub for sub in dirs if sub not in EXCLUDE_DIRS]
                for fname in sorted(files):
                    ext = os.path.splitext(fname)[1].lower()
                    if ext in EXCLUDE_EXTS:
                        continue
                    full_p = os.path.join(root, fname)
                    rel_p = os.path.relpath(full_p, ROOT_DIR).replace("\\", "/")
                    try:
                        with open(full_p, "rb") as f:
                            content = f.read()
                        f_hash = hashlib.sha256(content).hexdigest()
                        files_manifest[rel_p] = f_hash
                        hasher.update(rel_p.encode("utf-8"))
                        hasher.update(f_hash.encode("utf-8"))
                    except Exception:
                        pass

        for fname in ROOT_FILES:
            full_p = os.path.join(ROOT_DIR, fname)
            if os.path.isfile(full_p):
                rel_p = fname
                try:
                    with open(full_p, "rb") as f:
                        content = f.read()
                    f_hash = hashlib.sha256(content).hexdigest()
                    files_manifest[rel_p] = f_hash
                    hasher.update(rel_p.encode("utf-8"))
                    hasher.update(f_hash.encode("utf-8"))
                except Exception:
                    pass

        overall_hash = hasher.hexdigest()
        return {
            "version": "3.1.9-live",
            "overall_hash": overall_hash,
            "description": "v3.1.9: Fixed Take Control screen freeze by ensuring continuous frame streaming and returning cached JPEG bytes when pixels remain idle.",
            "file_count": len(files_manifest),
            "files": files_manifest
        }

    @staticmethod
    def generate_code_bundle() -> Tuple[bytes, str]:
        """Creates an in-memory zip bundle of all current live source code."""
        manifest = OTAManager.get_code_manifest()
        mem_file = io.BytesIO()

        with zipfile.ZipFile(mem_file, "w", zipfile.ZIP_DEFLATED) as zf:
            # Include manifest metadata
            zf.writestr("manifest.json", json.dumps(manifest, indent=2))

            for rel_path in manifest["files"].keys():
                full_path = os.path.join(ROOT_DIR, rel_path)
                if os.path.isfile(full_path):
                    zf.write(full_path, arcname=rel_path)

        mem_file.seek(0)
        return mem_file.getvalue(), manifest["overall_hash"]

    @staticmethod
    def get_payload_path(filename: str) -> Optional[str]:
        """Returns safe path to a dependency payload file if it exists."""
        clean_name = os.path.basename(filename)
        p = os.path.join(PAYLOADS_DIR, clean_name)
        return p if os.path.isfile(p) else None
