@echo off
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
