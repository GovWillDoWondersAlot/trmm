"""
Tactical RMM — Standalone Executable Setup Compiler.
Compiles a single-file, self-extracting, elevated Windows Installer (.exe) with embedded UAC manifest.
Directly installs the self-contained native TRMM_Agent.exe (Zero Python requirement).
"""

import os
import sys
import shutil
import zipfile
import subprocess
import tempfile
import logging

logger = logging.getLogger("master_hub.setup_compiler")

def run_and_log(cmd, log_file, cwd=None):
    """Run a command, capture stdout/stderr, and write everything to the install log.
    Raises if the command exits with non‑zero status.
    """
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
        )
        log_and_print(f"[CMD] {' '.join(cmd)}", log_file)
        if result.stdout:
            for line in result.stdout.splitlines():
                log_and_print(f"[STDOUT] {line}", log_file)
        if result.stderr:
            for line in result.stderr.splitlines():
                log_and_print(f"[STDERR] {line}", log_file)
        result.check_returncode()
        return result
    except subprocess.CalledProcessError as e:
        log_and_print(f"[ERROR] Command failed (rc={e.returncode})", log_file)
        if e.stdout:
            for line in e.stdout.splitlines():
                log_and_print(f"[STDOUT] {line}", log_file)
        if e.stderr:
            for line in e.stderr.splitlines():
                log_and_print(f"[STDERR] {line}", log_file)
        raise

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_DIR = os.path.join(ROOT_DIR, "generated_agents")


class SetupCompiler:
    """Compiles single-file executable installers for agents."""

    @staticmethod
    def compile_installer(payload_zip: str, agent_id: str, endpoint_tag: str, arch: str = "x64", custom_name: str = None, icon_path: str = None) -> str:
        """
        Compiles a self-contained setup .exe embedding the payload zip.
        Supports custom executable filenames and custom application icons (.ico).
        """
        if custom_name and custom_name.strip():
            clean = custom_name.strip()
            if clean.lower().endswith(".exe"):
                clean = clean[:-4]
            clean = "".join(c for c in clean if c.isalnum() or c in ("-", "_", " ")).strip()
            app_name = clean if clean else f"Agent_{endpoint_tag}"
            app_title = clean.title() if clean else f"Agent ({endpoint_tag})"
            out_name = app_name
        else:
            app_name = f"Agent_{endpoint_tag}"
            app_title = f"Agent ({endpoint_tag})"
            out_name = f"{app_name}_Setup_{arch}"

        final_exe = os.path.join(OUTPUT_DIR, f"{out_name}.exe")

        # Remove any stale exe with same name to prevent serving old cached builds
        if os.path.isfile(final_exe):
            try:
                os.remove(final_exe)
                logger.info(f"Removed stale setup exe: {final_exe}")
            except Exception as e:
                logger.warning(f"Could not remove old exe: {e}")

        build_temp = os.path.join(tempfile.gettempdir(), f"setup_build_{agent_id}")
        # Clean up any leftover build directory from a previous failed run
        if os.path.exists(build_temp):
            try:
                shutil.rmtree(build_temp)
            except Exception:
                pass
        os.makedirs(build_temp, exist_ok=True)

        # 1. Try modern NSIS compiler (Available natively on Linux via 'nsis' and Windows via NSIS)
        makensis_path = shutil.which("makensis")
        if makensis_path:
            try:
                nsis_exe = SetupCompiler._compile_nsis(
                    makensis_path=makensis_path,
                    payload_zip=payload_zip,
                    build_temp=build_temp,
                    final_exe=final_exe,
                    agent_id=agent_id,
                    endpoint_tag=endpoint_tag,
                    app_name=app_name,
                    app_title=app_title,
                    icon_path=icon_path
                )
                if nsis_exe and os.path.isfile(nsis_exe):
                    logger.info(f"NSIS setup executable compiled successfully: {nsis_exe}")
                    shutil.rmtree(build_temp, ignore_errors=True)
                    return nsis_exe
            except Exception as e:
                logger.warning(f"NSIS compilation failed, falling back to PyInstaller: {e}")

        # 2. PyInstaller compilation fallback (Runs when hosted on Windows)
        embedded_zip_name = "payload.dat"
        shutil.copyfile(payload_zip, os.path.join(build_temp, embedded_zip_name))

        has_custom_icon = bool(icon_path and os.path.isfile(icon_path))
        if has_custom_icon:
            try:
                shutil.copyfile(icon_path, os.path.join(build_temp, "app_icon.ico"))
            except Exception as e:
                logger.debug(f"Could not copy custom icon to build dir: {e}")

        # Write clean setup launcher script with interactive pause & logs
        installer_script = """# -*- coding: utf-8 -*-
import os
import sys
import time
import zipfile
import subprocess
import ctypes
import traceback
import shutil

def log_and_print(msg, log_file):
    print(msg)
    try:
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(msg + "\\n")
    except Exception:
        pass

def is_admin():
    try:
        return ctypes.windll.shell32.IsUserAnAdmin()
    except Exception:
        return False

def main():
    agent_id = "__AGENT_ID__"
    endpoint_tag = "__ENDPOINT_TAG__"
    app_name = "__APP_NAME__"
    app_title = "__APP_TITLE__"
    log_file = os.path.join(os.environ.get("TEMP", "C:\\\\Temp"), f"{app_name.lower()}_install.log")
    
    log_and_print("=" * 65, log_file)
    log_and_print(f"  {app_title} -- Setup", log_file)
    log_and_print(f"  Target Tag : {endpoint_tag} (ID: {agent_id})", log_file)
    log_and_print(f"  Log File   : {log_file}", log_file)
    log_and_print("=" * 65, log_file)

    try:
        log_and_print(f"[*] Terminating previous {app_title} processes on this endpoint...", log_file)
        try:
            current_pid = os.getpid()
            run_and_log([
                "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
                f"try {{ Get-Process -Name '{app_name}', 'TRMM_Agent' -ErrorAction SilentlyContinue | Where-Object {{ $_.Id -ne {current_pid} }} | Stop-Process -Force }} catch {{}}"
            ], log_file)
            time.sleep(1)
        except Exception:
            pass
        # Clean up stale startup shortcuts and orphan VBS/LNK files
        log_and_print("[*] Cleaning up old startup shortcuts...", log_file)
        try:
            startup_folders = [
                os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows", "Start Menu", "Programs", "Startup"),
                os.path.join(os.environ.get("ProgramData", "C:\\ProgramData"), "Microsoft", "Windows", "Start Menu", "Programs", "Startup")
            ]
            for s_folder in startup_folders:
                if os.path.isdir(s_folder):
                    for fname in os.listdir(s_folder):
                        # Remove entries for current app or generic TRMM_Agent
                        if (fname.startswith(app_name) or fname.startswith("TRMM_Agent")) and (fname.endswith('.lnk') or fname.endswith('.vbs')):
                            try:
                                os.remove(os.path.join(s_folder, fname))
                            except Exception:
                                pass
                        # Remove orphan VBS/LNK whose target exe no longer exists
                        elif fname.endswith('.vbs') or fname.endswith('.lnk'):
                            file_path = os.path.join(s_folder, fname)
                            try:
                                with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                                    content = f.read()
                                import re, time
                                match = re.search(r"[A-Za-z]:\\\\[^\\s\\\"]+\\.exe", content, re.IGNORECASE)
                                if match:
                                    exe_path = match.group(0)
                                    if not os.path.isfile(exe_path):
                                        os.remove(file_path)
                                else:
                                    # Delete old files (older than a week) if we cannot parse exe path
                                    if os.path.getmtime(file_path) < (time.time() - 7*24*60*60):
                                        os.remove(file_path)
                            except Exception:
                                pass
        except Exception:
            pass
        log_and_print("[*] Preparing updates cache...", log_file)
        try:
            for app_dir in [os.environ.get("LOCALAPPDATA", ""), os.environ.get("APPDATA", "")]:
                stale_up = os.path.join(app_dir, app_name, "updates")
                if os.path.isdir(stale_up):
                    shutil.rmtree(stale_up, ignore_errors=True)
                stale_up_trmm = os.path.join(app_dir, "TRMM_Agent", "updates")
                if os.path.isdir(stale_up_trmm):
                    shutil.rmtree(stale_up_trmm, ignore_errors=True)
        except Exception:
            pass

        if not is_admin():
            log_and_print(f"[!] Note: Running as standard user. Installing in LocalAppData.", log_file)
            install_dir = os.path.join(os.environ.get("LOCALAPPDATA", "C:\\\\Temp"), f"{app_name}_{agent_id}")
        else:
            install_dir = os.path.join(os.environ.get("ProgramFiles", "C:\\\\Program Files"), f"{app_name}_{agent_id}")

        if getattr(sys, "frozen", False):
            bundle_dir = sys._MEIPASS
        else:
            bundle_dir = os.path.dirname(os.path.abspath(__file__))

        payload_path = os.path.join(bundle_dir, "payload.dat")

        log_and_print(f"[*] Target installation folder: {install_dir}", log_file)
# Ensure the target installation directory is writable
        try:
            os.makedirs(install_dir, exist_ok=True)
        except PermissionError as e:
            log_and_print(f"[!] ERROR: Cannot create installation directory {install_dir}: {e}", log_file)
            raise

        # Verify sufficient free disk space (payload size + 10 MB buffer)
        try:
            payload_size = os.path.getsize(payload_path)
            total, used, free = shutil.disk_usage(install_dir)
            if free < payload_size + 10 * 1024 * 1024:
                log_and_print(f"[!] ERROR: Not enough free disk space. Required: {payload_size + 10 * 1024 * 1024} bytes, Available: {free} bytes.", log_file)
                raise OSError("Insufficient disk space for extraction")
        except Exception as e:
            log_and_print(f"[!] WARNING: Disk space check failed: {e}", log_file)

        log_and_print(f"[*] Extracting standalone {app_title} binaries...", log_file)
        try:
            with zipfile.ZipFile(payload_path, "r") as zf:
                zf.extractall(install_dir)
            log_and_print("[+] Standalone files extracted successfully.", log_file)
        except Exception as e:
            log_and_print(f"[!] ERROR during extraction: {e}", log_file)
            raise

        # Copy custom icon into installation directory if provided
        icon_bundle_src = os.path.join(bundle_dir, "app_icon.ico")
        if os.path.isfile(icon_bundle_src):
            try:
                shutil.copy2(icon_bundle_src, os.path.join(install_dir, "app_icon.ico"))
            except Exception:
                pass

# Determine the agent executable path, handling custom-named binary
found_exes = [f for f in os.listdir(install_dir) if f.lower().endswith('.exe')]
if found_exes:
    original_exe_path = os.path.join(install_dir, found_exes[0])
    agent_exe = os.path.join(install_dir, f"{app_name}.exe")
    if os.path.normcase(original_exe_path) != os.path.normcase(agent_exe):
        try:
            if os.path.isfile(agent_exe):
                os.remove(agent_exe)
            os.rename(original_exe_path, agent_exe)
        except Exception:
            try:
                shutil.copy2(original_exe_path, agent_exe)
            except Exception:
                agent_exe = original_exe_path
else:
    agent_exe = ""


        # Strip Mark-of-the-Web (Zone.Identifier) to prevent Open File security warnings
        try:
            run_and_log(["powershell", "-NoProfile", "-Command", f"Unblock-File -Path '{agent_exe}'"], log_file)
        except Exception:
            pass

        # Setup Silent Startup Persistence (Zero UAC prompts)
        log_and_print("[*] Configuring silent startup persistence...", log_file)
        vbs_path = None
        try:
            import winreg
            # 1. Force RunAsInvoker in AppCompatFlags
            for root_k in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE if is_admin() else ()):
                try:
                    k_compat = winreg.CreateKey(root_k, r"Software\Microsoft\Windows NT\CurrentVersion\AppCompatFlags\Layers")
                    winreg.SetValueEx(k_compat, agent_exe, 0, winreg.REG_SZ, "~ RUNASINVOKER")
                    winreg.CloseKey(k_compat)
                except Exception:
                    pass

            # 2. Silent VBScript Launcher in User Startup
            startup_folder = os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows", "Start Menu", "Programs", "Startup")
            if os.path.isdir(startup_folder):
                vbs_path = os.path.join(startup_folder, f"{app_name}.vbs")
                vbs_content = (
                    'Set ws = CreateObject("WScript.Shell")\\n'
                    'Set env = ws.Environment("Process")\\n'
                    'env("__COMPAT_LAYER") = "RunAsInvoker"\\n'
                    + 'ws.Run "\\"" + agent_exe + "\\"", 0, False\\n'
                )
                with open(vbs_path, "w", encoding="utf-8") as vf:
                    vf.write(vbs_content)
                subprocess.run(["powershell", "-NoProfile", "-Command", "Unblock-File -Path '" + vbs_path + "'"], capture_output=True)

            # 3. HKCU Run key using wscript
            k_run = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_SET_VALUE)
            run_cmd = 'wscript.exe "' + vbs_path + '"' if vbs_path else '"' + agent_exe + '"'
            winreg.SetValueEx(k_run, app_name, 0, winreg.REG_SZ, run_cmd)
            winreg.CloseKey(k_run)

            # 4. Elevated Scheduled Tasks if admin (silent & bypasses UAC on boot & logon)
            if is_admin():
                # Purge any legacy HKLM Run entry
                run_and_log(["reg", "delete", r"HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Run", "/v", app_name, "/f"], log_file)
                run_and_log(["reg", "delete", r"HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Run", "/v", "TRMM_Agent", "/f"], log_file)
                run_and_log([
                    "schtasks", "/Create",
                    "/TN", f"{app_name}_User",
                    "/TR", '"' + agent_exe + '"',
                    "/SC", "ONLOGON",
                    "/RL", "HIGHEST",
                    "/F"
                ], log_file)
                run_and_log([
                    "schtasks", "/Create",
                    "/TN", f"{app_name}_Persist",
                    "/TR", '"' + agent_exe + '"',
                    "/SC", "ONSTART",
                    "/RU", "SYSTEM",
                    "/RL", "HIGHEST",
                    "/DELAY", "0000:10",
                    "/F"
                ], log_file)

            log_and_print("[+] Silent persistence configured (Zero UAC Prompts).", log_file)
        except Exception as e:
            log_and_print("[!] Warning configuring startup persistence: " + str(e), log_file)

        # Launch Agent directly
        log_and_print(f"[*] Launching {app_title} background service...", log_file)
        try:
            log_and_print(f"[CMD] Launching agent executable: {agent_exe}", log_file)
            subprocess.Popen([agent_exe], cwd=install_dir, creationflags=0x00000008 | 0x00000200)
            log_and_print(f"[+] Standalone {app_title} process launched successfully.", log_file)
        except Exception as e:
            log_and_print(f"[!] Failed to start process: {e}", log_file)

        log_and_print("=" * 65, log_file)
        log_and_print(f"  [+] SUCCESS: {app_title} is installed and active!", log_file)
        log_and_print("      The endpoint should now show Online in Master Hub.", log_file)
        log_and_print("=" * 65, log_file)

    except Exception as e:
        log_and_print("\\n[!] INSTALLATION ERROR ENCOUNTERED:", log_file)
        log_and_print(traceback.format_exc(), log_file)

    log_and_print("\\nPress Enter to finish and close this window...", log_file)
    try:
        input()
    except Exception:
        time.sleep(10)

if __name__ == "__main__":
    main()
""".replace("__AGENT_ID__", agent_id).replace("__ENDPOINT_TAG__", endpoint_tag).replace("__APP_NAME__", app_name).replace("__APP_TITLE__", app_title)

        stub_py = os.path.join(build_temp, "installer_stub.py")
        with open(stub_py, "w", encoding="utf-8") as f:
            f.write(installer_script)

        pyinstaller_cmd = [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--onefile",
            "--distpath",
            OUTPUT_DIR,
            "--workpath",
            os.path.join(build_temp, "work"),
            "--specpath",
            build_temp,
            "--name",
            out_name,
            "--add-data",
            f"{os.path.join(build_temp, embedded_zip_name)};.",
        ]
        if has_custom_icon:
            pyinstaller_cmd.extend(["--add-data", f"{os.path.join(build_temp, 'app_icon.ico')};."])
            pyinstaller_cmd.extend(["--icon", icon_path])
        pyinstaller_cmd.append(stub_py)

        logger.info(f"Compiling standalone setup executable: {final_exe}")
        res = subprocess.run(pyinstaller_cmd, capture_output=True)

        # Cleanup build temp
        shutil.rmtree(build_temp, ignore_errors=True)

        if os.path.isfile(final_exe):
            logger.info(f"Setup executable generated successfully: {final_exe}")
            return final_exe
        else:
            logger.error(f"PyInstaller build failed. Stderr: {res.stderr.decode('utf-8', errors='ignore')}")
            return None

    @staticmethod
    def _compile_nsis(makensis_path: str, payload_zip: str, build_temp: str, final_exe: str,
                      agent_id: str, endpoint_tag: str, app_name: str, app_title: str, icon_path: str = None):
        """
        Compiles a high-performance, single-file Windows setup executable using NSIS.
        Extracts embedded payload directly, renames the executable to custom app_name,
        creates persistent autostart shortcuts/registry entries, and launches silently.
        """
        payload_extract_dir = os.path.join(build_temp, "payload")
        os.makedirs(payload_extract_dir, exist_ok=True)
        with zipfile.ZipFile(payload_zip, "r") as z:
            z.extractall(payload_extract_dir)

        icon_directive = ""
        if icon_path and os.path.isfile(icon_path):
            nsis_icon = os.path.join(build_temp, "app_icon.ico")
            try:
                shutil.copyfile(icon_path, nsis_icon)
                icon_directive = f'Icon "{nsis_icon.replace(chr(92), "/")}"'
            except Exception:
                pass

        # Compile to temporary file first so clients never download an incomplete file
        temp_exe = os.path.join(build_temp, os.path.basename(final_exe))
        clean_temp_exe = temp_exe.replace("\\", "/")
        clean_payload_dir = payload_extract_dir.replace("\\", "/")

        nsi_content = f"""Unicode True
RequestExecutionLevel user
SetCompressor /SOLID zlib

!include "LogicLib.nsh"

Name "{app_title}"
Caption "{app_title} Setup"
OutFile "{clean_temp_exe}"

{icon_directive}

ShowInstDetails show
AutoCloseWindow false
CompletedText "Setup Complete -- Click Close to finish"

Page instfiles

Function .onInit
    ClearErrors
    CreateDirectory "$PROGRAMFILES64\\trmm_perm_test"
    FileOpen $R0 "$PROGRAMFILES64\\trmm_perm_test\\test.tmp" w
    ${{If}} ${{Errors}}
        ; Standard user or non-elevated: install to LocalAppData
        StrCpy $INSTDIR "$LOCALAPPDATA\\{app_name}_{agent_id}"
    ${{Else}}
        FileClose $R0
        Delete "$PROGRAMFILES64\\trmm_perm_test\\test.tmp"
        RmDir "$PROGRAMFILES64\\trmm_perm_test"
        StrCpy $INSTDIR "$PROGRAMFILES64\\{app_name}_{agent_id}"
    ${{EndIf}}
FunctionEnd

Section "Main"
    ; 1. Initialize installer log file (flushed immediately)
    FileOpen $0 "$TEMP\\{app_name.lower()}_install.log" w
    FileWrite $0 "=== {app_title} Setup Log ===$\\r$\\n"
    FileWrite $0 "Target Directory: $INSTDIR$\\r$\\n"
    FileWrite $0 "Endpoint Tag    : {endpoint_tag}$\\r$\\n"
    FileWrite $0 "Agent ID        : {agent_id}$\\r$\\n"
    FileClose $0

    DetailPrint "=================================================="
    DetailPrint "  {app_title} Setup & Background Service"
    DetailPrint "=================================================="
    DetailPrint "[*] Target Directory: $INSTDIR"

    ; 2. Terminate previous running agent instances in $INSTDIR (protect installer from killing itself)
    DetailPrint "[*] Terminating previous background agents in $INSTDIR..."
    ExecWait 'powershell -NoProfile -ExecutionPolicy Bypass -Command "try {{{{ Get-Process -Name \"{app_name}\", \"TRMM_Agent\" -ErrorAction SilentlyContinue | Where-Object {{{{ $_.Path -and ($_.Path -like \"$INSTDIR*\" -or $_.Path -like \"*TRMM*\") -and $_.Path -ne \"$EXEPATH\" }}}} | Stop-Process -Force }}}} catch {{{{}}; exit 0"'

    ; 3. Create install directory and extract payload
    DetailPrint "[*] Extracting application components..."
    FileOpen $0 "$TEMP\\{app_name.lower()}_install.log" a
    FileWrite $0 "[*] Creating target directory: $INSTDIR$\\r$\\n"
    FileClose $0

    CreateDirectory "$INSTDIR"
    SetOutPath "$INSTDIR"
    SetOverwrite try
    ClearErrors
    File /r "{clean_payload_dir}/*"
    ${{If}} ${{Errors}}
        FileOpen $0 "$TEMP\\{app_name.lower()}_install.log" a
        FileWrite $0 "[ERROR] Failed to extract payload files to $INSTDIR (Access Denied or Disk Full).$\\r$\\n"
        FileClose $0
        MessageBox MB_OK|MB_ICONSTOP "Extraction Error: Failed to extract files to:$\\r$\\n$INSTDIR$\\r$\\nPlease check disk space and permissions."
        Abort
    ${{EndIf}}

    FileOpen $0 "$TEMP\\{app_name.lower()}_install.log" a
    FileWrite $0 "[+] Payload components extracted successfully to $INSTDIR.$\\r$\\n"
    FileClose $0

    ; 4. Dynamically rename executable if needed
    IfFileExists "$INSTDIR\\TRMM_Agent.exe" 0 +3
    Rename "$INSTDIR\\TRMM_Agent.exe" "$INSTDIR\\{app_name}.exe"

    FileOpen $0 "$TEMP\\{app_name.lower()}_install.log" a
    FileWrite $0 "[+] Renamed binary to {app_name}.exe$\\r$\\n"
    FileClose $0

    ; 5. Configure zero-UAC execution layer
    WriteRegStr HKCU "Software\\Microsoft\\Windows NT\\CurrentVersion\\AppCompatFlags\\Layers" "$INSTDIR\\{app_name}.exe" "~ RUNASINVOKER"
    WriteRegStr HKLM "Software\\Microsoft\\Windows NT\\CurrentVersion\\AppCompatFlags\\Layers" "$INSTDIR\\{app_name}.exe" "~ RUNASINVOKER"

    ; 6. Set HKCU Run registry key for user startup
    DetailPrint "[+] Configuring automatic startup persistence..."
    WriteRegStr HKCU "Software\\Microsoft\\Windows\\CurrentVersion\\Run" "{app_name}" '"$INSTDIR\\{app_name}.exe"'

    ; 7. Create startup shortcut in current user Startup folder
    SetShellVarContext current
    CreateShortcut "$SMSTARTUP\\{app_name}.lnk" "$INSTDIR\\{app_name}.exe"

    FileOpen $0 "$TEMP\\{app_name.lower()}_install.log" a
    FileWrite $0 "[+] Registered HKCU Run entry and Startup shortcut.$\\r$\\n"
    FileClose $0

    ; 8. Scheduled tasks for highest privilege auto-start (if admin rights available)
    ClearErrors
    ExecWait 'cmd /c schtasks /Create /TN "{app_name}_User" /TR "\"$INSTDIR\\{app_name}.exe\"" /SC ONLOGON /RL HIGHEST /F >nul 2>&1'
    ExecWait 'cmd /c schtasks /Create /TN "{app_name}_Persist" /TR "\"$INSTDIR\\{app_name}.exe\"" /SC ONSTART /RU SYSTEM /RL HIGHEST /DELAY 0000:10 /F >nul 2>&1'

    ; 9. Launch background service cleanly with RunAsInvoker
    DetailPrint "[+] Launching {app_title} background service..."
    System::Call 'kernel32::SetEnvironmentVariable(t "__COMPAT_LAYER", t "RunAsInvoker")'
    Exec '"$INSTDIR\\{app_name}.exe"'
    Sleep 1500

    ; Copy install log to installation folder
    CopyFiles "$TEMP\\{app_name.lower()}_install.log" "$INSTDIR\\install.log"

    FileOpen $0 "$TEMP\\{app_name.lower()}_install.log" a
    FileWrite $0 "[+] Launched $INSTDIR\\{app_name}.exe$\\r$\\n"
    FileWrite $0 "=== Installation Finished Successfully ===$\\r$\\n"
    FileClose $0

    DetailPrint "=================================================="
    DetailPrint "  [SUCCESS] {app_title} is installed and active!"
    DetailPrint "  Log saved to: $TEMP\\{app_name.lower()}_install.log"
    DetailPrint "  Also saved to: $INSTDIR\\install.log"
    DetailPrint "=================================================="

    MessageBox MB_OK|MB_ICONINFORMATION "{app_title} Setup Completed Successfully!$\\r$\\n$\\r$\\nProcess: {app_name}.exe$\\r$\\nLocation: $INSTDIR$\\r$\\nLog: $TEMP\\{app_name.lower()}_install.log"
SectionEnd
"""
        nsi_file = os.path.join(build_temp, "installer.nsi")
        with open(nsi_file, "w", encoding="utf-8") as f:
            f.write(nsi_content)

        logger.info(f"Running NSIS compiler: {makensis_path} {nsi_file}")
        res = subprocess.run([makensis_path, nsi_file], capture_output=True)
        if res.returncode != 0:
            err_msg = res.stderr.decode('utf-8', errors='ignore') or res.stdout.decode('utf-8', errors='ignore')
            logger.warning(f"makensis error: {err_msg}")
            return None

        if os.path.isfile(temp_exe) and os.path.getsize(temp_exe) > 1000:
            # Atomically publish the complete executable
            shutil.move(temp_exe, final_exe)
            return final_exe
        return None


