@echo off
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
