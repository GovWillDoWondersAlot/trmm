"""
Builds the base Android APK template (master_hub/payloads/android_agent_base.apk).
Uses Android SDK build-tools and JDK located on the system.
"""

import os
import sys
import subprocess
import shutil

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ANDROID_DIR = os.path.join(ROOT_DIR, "master_hub", "android")
PAYLOADS_DIR = os.path.join(ROOT_DIR, "master_hub", "payloads")
os.makedirs(PAYLOADS_DIR, exist_ok=True)

# Locate Android SDK
SDK_DIR = os.path.expanduser(r"~\AppData\Local\Android\Sdk")
BUILD_TOOLS_DIR = os.path.join(SDK_DIR, "build-tools", "34.0.0")
if not os.path.isdir(BUILD_TOOLS_DIR):
    BUILD_TOOLS_DIR = os.path.join(SDK_DIR, "build-tools", "35.0.0")

PLATFORM_DIR = os.path.join(SDK_DIR, "platforms", "android-34")
if not os.path.isdir(PLATFORM_DIR):
    PLATFORM_DIR = os.path.join(SDK_DIR, "platforms", "android-35")
ANDROID_JAR = os.path.join(PLATFORM_DIR, "android.jar")

AAPT2 = os.path.join(BUILD_TOOLS_DIR, "aapt2.exe")
D8 = os.path.join(BUILD_TOOLS_DIR, "d8.bat")
APKSIGNER = os.path.join(BUILD_TOOLS_DIR, "apksigner.bat")
ZIPALIGN = os.path.join(BUILD_TOOLS_DIR, "zipalign.exe")

JAVAC = r"C:\Program Files\Android\Android Studio\jbr\bin\javac.exe"
KEYTOOL = r"C:\Program Files\Android\Android Studio\jbr\bin\keytool.exe"

WORK_DIR = os.path.join(ANDROID_DIR, "work")

def run_cmd(cmd, cwd=None):
    print("Running:", " ".join(cmd) if isinstance(cmd, list) else cmd)
    res = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, shell=isinstance(cmd, str))
    if res.returncode != 0:
        print("STDOUT:", res.stdout)
        print("STDERR:", res.stderr)
        raise RuntimeError(f"Command failed with code {res.returncode}")
    return res

def build_base_apk():
    print("[*] Starting Base Android APK Compilation...")
    if os.path.exists(WORK_DIR):
        shutil.rmtree(WORK_DIR)
    os.makedirs(WORK_DIR, exist_ok=True)

    compiled_res = os.path.join(WORK_DIR, "compiled_res")
    os.makedirs(compiled_res, exist_ok=True)
    gen_dir = os.path.join(WORK_DIR, "gen")
    os.makedirs(gen_dir, exist_ok=True)
    classes_dir = os.path.join(WORK_DIR, "classes")
    os.makedirs(classes_dir, exist_ok=True)

    res_dir = os.path.join(ANDROID_DIR, "res")
    manifest = os.path.join(ANDROID_DIR, "AndroidManifest.xml")

    # 1. Compile resources with aapt2
    run_cmd([AAPT2, "compile", "--dir", res_dir, "-o", os.path.join(compiled_res, "resources.zip")])

    # 2. Link resources with aapt2
    unaligned_apk = os.path.join(WORK_DIR, "unaligned.apk")
    run_cmd([
        AAPT2, "link",
        "-o", unaligned_apk,
        "-I", ANDROID_JAR,
        "--manifest", manifest,
        "--java", gen_dir,
        "--auto-add-overlay",
        os.path.join(compiled_res, "resources.zip")
    ])

    # 3. Find all java sources (including generated R.java)
    java_files = []
    src_dir = os.path.join(ANDROID_DIR, "src")
    for root, _, files in os.walk(src_dir):
        for f in files:
            if f.endswith(".java"):
                java_files.append(os.path.join(root, f))
    for root, _, files in os.walk(gen_dir):
        for f in files:
            if f.endswith(".java"):
                java_files.append(os.path.join(root, f))

    print(f"[*] Found {len(java_files)} Java files to compile.")

    # 4. Compile Java sources with javac
    javac_cmd = [JAVAC, "--release", "8", "-cp", ANDROID_JAR, "-d", classes_dir] + java_files
    run_cmd(javac_cmd)

    # 5. Compile bytecode to dex using D8
    class_files = []
    for root, _, files in os.walk(classes_dir):
        for f in files:
            if f.endswith(".class"):
                class_files.append(os.path.join(root, f))

    d8_cmd = [D8, "--lib", ANDROID_JAR, "--output", WORK_DIR] + class_files
    run_cmd(d8_cmd)

    classes_dex = os.path.join(WORK_DIR, "classes.dex")
    if not os.path.isfile(classes_dex):
        raise RuntimeError("D8 failed to generate classes.dex")

    # Save classes.dex to payloads for fast repackaging
    shutil.copyfile(classes_dex, os.path.join(PAYLOADS_DIR, "classes.dex"))
    print(f"[*] Updated {os.path.join(PAYLOADS_DIR, 'classes.dex')}")

    # 6. Add classes.dex into unaligned_apk
    import zipfile
    with zipfile.ZipFile(unaligned_apk, "a", zipfile.ZIP_DEFLATED) as apk_zip:
        apk_zip.write(classes_dex, "classes.dex")
        # Add default template config.json
        import json
        default_cfg = {
            "server_url": "ws://127.0.0.1:8000",
            "agent_id": "android_template",
            "endpoint_tag": "Android-Device",
            "auto_start": True
        }
        apk_zip.writestr("assets/config.json", json.dumps(default_cfg, indent=2))

    # 7. Zipalign
    aligned_apk = os.path.join(WORK_DIR, "aligned.apk")
    run_cmd([ZIPALIGN, "-f", "-p", "4", unaligned_apk, aligned_apk])

    # 8. Sign APK with debug keystore
    keystore_path = os.path.expanduser(r"~\.android\debug.keystore")
    if not os.path.isfile(keystore_path):
        # Generate self-signed keystore
        keystore_path = os.path.join(ANDROID_DIR, "trmm_debug.keystore")
        if not os.path.isfile(keystore_path):
            run_cmd([
                KEYTOOL, "-genkeypair", "-v",
                "-keystore", keystore_path,
                "-alias", "trmm_agent",
                "-keyalg", "RSA", "-keysize", "2048",
                "-validity", "10000",
                "-storepass", "android",
                "-keypass", "android",
                "-dname", "CN=Tactical RMM, OU=IT, O=TacticalRMM, L=US, S=CA, C=US"
            ])

    final_base_apk = os.path.join(PAYLOADS_DIR, "android_agent_base.apk")
    run_cmd([
        APKSIGNER, "sign",
        "--ks", keystore_path,
        "--ks-pass", "pass:android",
        "--out", final_base_apk,
        aligned_apk
    ])

    print(f"[OK] Base Android APK successfully built: {final_base_apk} ({os.path.getsize(final_base_apk)} bytes)")
    shutil.rmtree(WORK_DIR, ignore_errors=True)

if __name__ == "__main__":
    build_base_apk()
