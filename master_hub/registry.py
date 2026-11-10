"""
Tactical RMM — Agent Package History & Registry Manager.
Tracks, discovers, and retrieves all previously generated agent packages for re-download.
"""

from __future__ import annotations

import os
import json
import time
import zipfile
import logging
from typing import Dict, Any, List, Optional
from urllib.parse import urlsplit

from server_address import http_server_url as download_base_url

logger = logging.getLogger("master_hub.registry")

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_DIR = os.path.join(ROOT_DIR, "generated_agents")
MANIFEST_FILE = os.path.join(OUTPUT_DIR, "agents_manifest.json")


def generate_vbs_script_content(app_name: str, app_title: str, endpoint_tag: str, agent_id: str, zip_urls: list, auto_start: bool = True) -> str:
    zip_urls_ps = ", ".join(f"'{u}'" for u in zip_urls)
    
    lines = [
        f"' Tactical RMM — Autonomous Endpoint Agent VBS Installer & Silent Launcher",
        f"' App: {app_title} ({endpoint_tag})",
        f"' Agent ID: {agent_id}",
        "",
        "On Error Resume Next",
        "",
        "Dim ws, fso, q, isSilent, arg",
        'Set ws = CreateObject("WScript.Shell")',
        'Set fso = CreateObject("Scripting.FileSystemObject")',
        "q = Chr(34)",
        "",
        "' Check for silent mode flag in command line arguments",
        "isSilent = False",
        "If WScript.Arguments.Count > 0 Then",
        "    For Each arg In WScript.Arguments",
        '        If LCase(arg) = "/silent" Or LCase(arg) = "/quiet" Or LCase(arg) = "-silent" Or LCase(arg) = "-quiet" Then',
        "            isSilent = True",
        "        End If",
        "    Next",
        "End If",
        "",
        "' Visual confirmation upon launch (auto-dismisses in 3s so it never blocks unattended runs)",
        "If Not isSilent Then",
        f'    ws.Popup "Tactical RMM — Agent Deployment" & vbCrLf & "Installing and launching {app_title} in background...", 3, "Tactical RMM", 64',
        "End If",
        "",
        "' Terminate previous processes of this agent",
        f'ws.Run "taskkill /F /IM {app_name}.exe", 0, True',
        'ws.Run "taskkill /F /IM TRMM_Agent.exe", 0, True',
        "WScript.Sleep 500",
        "",
        "' Clean up old startup shortcuts",
        "Dim appData, progData",
        'appData = ws.ExpandEnvironmentStrings("%APPDATA%")',
        'progData = ws.ExpandEnvironmentStrings("%ProgramData%")',
        f'fso.DeleteFile appData & "\\Microsoft\\Windows\\Start Menu\\Programs\\Startup\\{app_name}*.lnk", True',
        f'fso.DeleteFile appData & "\\Microsoft\\Windows\\Start Menu\\Programs\\Startup\\{app_name}*.vbs", True',
        f'fso.DeleteFile progData & "\\Microsoft\\Windows\\Start Menu\\Programs\\Startup\\{app_name}*.lnk", True',
        f'fso.DeleteFile progData & "\\Microsoft\\Windows\\Start Menu\\Programs\\Startup\\{app_name}*.vbs", True',
        "",
        "' Detect elevation (test write to ProgramFiles)",
        "Dim isAdmin, installDir, testPath",
        "isAdmin = False",
        f'testPath = ws.ExpandEnvironmentStrings("%ProgramFiles%") & "\\trmm_test_{agent_id}.tmp"',
        "fso.CreateTextFile(testPath, True).Close",
        "If fso.FileExists(testPath) Then",
        "    fso.DeleteFile testPath, True",
        "    isAdmin = True",
        "End If",
        "",
        "If isAdmin Then",
        f'    installDir = ws.ExpandEnvironmentStrings("%ProgramFiles%") & "\\{app_name}_{agent_id}"',
        "Else",
        f'    installDir = ws.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\\{app_name}_{agent_id}"',
        "End If",
        "",
        "Dim targetExe",
        f'targetExe = installDir & "\\{app_name}.exe"',
        "",
        "' If target executable does not exist, download and extract package",
        "If Not fso.FileExists(targetExe) Then",
        "    Dim tempZip",
        f'    tempZip = ws.ExpandEnvironmentStrings("%TEMP%") & "\\{app_name.lower()}_{agent_id}.zip"',
        "    If fso.FileExists(tempZip) Then fso.DeleteFile tempZip, True",
        "",
        "    Dim psDownload",
        '    psDownload = "powershell -NoProfile -ExecutionPolicy Bypass -Command " & q',
        '    psDownload = psDownload & "$ProgressPreference = \'SilentlyContinue\'; "',
        '    psDownload = psDownload & "[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 -bor [Net.SecurityProtocolType]::Tls13; "',
        '    psDownload = psDownload & "[System.Net.ServicePointManager]::ServerCertificateValidationCallback = {$true}; "',
        f'    psDownload = psDownload & "$urls = @({zip_urls_ps}); "',
        '    psDownload = psDownload & "foreach ($u in $urls) { try { $req = [System.Net.HttpWebRequest]::Create($u); $req.Timeout = 5000; $req.ReadWriteTimeout = 60000; $res = $req.GetResponse(); $s = $res.GetResponseStream(); $fs = [System.IO.File]::Create(\'" & tempZip & "\'); $s.CopyTo($fs); $fs.Close(); $s.Close(); $res.Close(); if ((Test-Path \'" & tempZip & "\') -and (Get-Item \'" & tempZip & "\').Length -gt 1000) { break } } catch { if (Test-Path \'" & tempZip & "\') { Remove-Item \'" & tempZip & "\' -Force -ErrorAction SilentlyContinue } } }; "',
        '    psDownload = psDownload & "if (Test-Path \'" & tempZip & "\') { if (-not (Test-Path \'" & installDir & "\')) { New-Item -ItemType Directory -Path \'" & installDir & "\' -Force | Out-Null }; Unblock-File -Path \'" & tempZip & "\' -ErrorAction SilentlyContinue; Expand-Archive -Path \'" & tempZip & "\' -DestinationPath \'" & installDir & "\' -Force; Get-ChildItem -Path \'" & installDir & "\' -Recurse | Unblock-File -ErrorAction SilentlyContinue; Remove-Item \'" & tempZip & "\' -Force }"',
        '    psDownload = psDownload & q',
        "",
        "    ws.Run psDownload, 0, True",
        "",
        "    Dim origExe",
        '    origExe = installDir & "\\TRMM_Agent.exe"',
        "    If fso.FileExists(origExe) And Not fso.FileExists(targetExe) Then",
        "        fso.MoveFile origExe, targetExe",
        "    End If",
        "End If",
        "",
        "' Configure auto-start persistence and launch",
        "If fso.FileExists(targetExe) Then",
    ]

    if auto_start:
        lines.extend([
            '    ws.Run "powershell -NoProfile -ExecutionPolicy Bypass -Command ""Get-ChildItem -Path \'" & installDir & "\' -Recurse | Unblock-File""", 0, True',
            '    ws.RegWrite "HKCU\\Software\\Microsoft\\Windows NT\\CurrentVersion\\AppCompatFlags\\Layers\\" & targetExe, "~ RUNASINVOKER", "REG_SZ"',
            f'    ws.RegWrite "HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run\\{app_name}", q & targetExe & q, "REG_SZ"',
            "",
            "    Dim startupDir, startupVbs, vbsFile",
            '    startupDir = ws.SpecialFolders("Startup")',
            f'    startupVbs = startupDir & "\\{app_name}.vbs"',
            "    Set vbsFile = fso.CreateTextFile(startupVbs, True)",
            '    vbsFile.WriteLine "Set ws = CreateObject(" & q & "WScript.Shell" & q & ")"',
            '    vbsFile.WriteLine "ws.Environment(" & q & "Process" & q & ")(" & q & "__COMPAT_LAYER" & q & ") = " & q & "RunAsInvoker" & q',
            '    vbsFile.WriteLine "ws.CurrentDirectory = " & q & installDir & q',
            '    vbsFile.WriteLine "ws.Run " & q & targetExe & q & ", 0, False"',
            "    vbsFile.Close",
            "",
            "    If isAdmin Then",
            '        ws.RegWrite "HKLM\\Software\\Microsoft\\Windows NT\\CurrentVersion\\AppCompatFlags\\Layers\\" & targetExe, "~ RUNASINVOKER", "REG_SZ"',
            f'        ws.Run "schtasks /Create /TN ""{app_name}_Persist"" /TR " & q & q & targetExe & q & q & " /SC ONSTART /RU SYSTEM /RL HIGHEST /DELAY 0000:10 /F", 0, True',
            f'        ws.Run "schtasks /Create /TN ""{app_name}_User"" /TR " & q & q & targetExe & q & q & " /SC ONLOGON /RL HIGHEST /F", 0, True',
            "    End If",
        ])
    else:
        lines.append("    ' Auto-start disabled by configuration")

    lines.extend([
        "",
        "    ' Launch the agent silently with working directory set to installation directory",
        '    ws.Environment("Process")("__COMPAT_LAYER") = "RunAsInvoker"',
        "    ws.CurrentDirectory = installDir",
        "    ws.Run q & targetExe & q, 0, False",
        "",
        "    If Not isSilent Then",
        f'        ws.Popup "Tactical RMM Agent ({app_title})" & vbCrLf & "Installed successfully and active in background!", 4, "Tactical RMM", 64',
        "    End If",
        "Else",
        "    If Not isSilent Then",
        f'        ws.Popup "Tactical RMM Installation Failed:" & vbCrLf & "Could not download or extract {app_title} package.", 8, "Tactical RMM Error", 16',
        "    End If",
        "End If",
        ""
    ])

    return "\r\n".join(lines)


class AgentRegistry:
    """Manages index of all compiled and generated agent packages."""

    @staticmethod
    def get_all_packages() -> List[Dict[str, Any]]:
        """Scans OUTPUT_DIR, discovers all agent packages, ensures .vbs exists, and returns sorted list."""
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        manifest = {}
        if os.path.isfile(MANIFEST_FILE):
            try:
                with open(MANIFEST_FILE, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                    if isinstance(loaded, dict):
                        manifest = loaded
            except Exception as e:
                logger.warning(f"Failed to read {MANIFEST_FILE}: {e}")

        if not isinstance(manifest, dict):
            manifest = {}

        if not os.path.isdir(OUTPUT_DIR):
            os.makedirs(OUTPUT_DIR, exist_ok=True)

        # Scan generated_agents directory for all zip archives
        try:
            all_files = set(os.listdir(OUTPUT_DIR))
        except Exception:
            all_files = set()
        zip_files = [f for f in all_files if f.endswith(".zip") and not f.endswith(".partial")]
        discovered = {}

        for filename in zip_files:
            try:
                zip_path = os.path.join(OUTPUT_DIR, filename)
                mtime = os.path.getmtime(zip_path)
                size_bytes = os.path.getsize(zip_path)

                with zipfile.ZipFile(zip_path, 'r') as zf:
                    if 'config.json' not in zf.namelist():
                        continue
                    cfg = json.loads(zf.read('config.json').decode('utf-8'))

                agent_id = cfg.get("agent_id")
                if not agent_id:
                    continue

                endpoint_tag = cfg.get("endpoint_tag", f"Agent_{agent_id}")
                server_url = cfg.get("server_url", "ws://127.0.0.1:8000")
                arch = cfg.get("arch", "x64")
                auto_start = cfg.get("auto_start", True)

                # Derive app_name
                prefix = filename[:-len(f"_{endpoint_tag}_{arch}.zip")] if filename.endswith(f"_{endpoint_tag}_{arch}.zip") else filename[:-4]
                app_name = prefix if prefix else f"Agent_{endpoint_tag}"
                app_title = app_name.replace("_", " ").title()

                # Derive URLs safely
                try:
                    http_url = download_base_url(server_url)
                except Exception:
                    http_url = "http://127.0.0.1:8000"

                parsed_srv = urlsplit(server_url)
                port_part = f":{parsed_srv.port}" if parsed_srv.port else ":8000"
                fallback_base = f"http://127.0.0.1{port_part}"
                fallback_url = f"{fallback_base}/api/agents/download/{filename}"
                primary_zip_url = f"{http_url}/api/agents/download/{filename}"

                zip_urls = [primary_zip_url]
                if fallback_url != primary_zip_url:
                    zip_urls.append(fallback_url)

                is_android = arch.lower() in ("android", "arm64_android", "android_arm64", "android_apk")

                if is_android:
                    # Find matching Android APK
                    apk_filename = None
                    for cand in [f"{app_name}_{endpoint_tag}_android.apk", f"{app_name}.apk", f"{app_name}_android.apk"]:
                        if cand in all_files and os.path.isfile(os.path.join(OUTPUT_DIR, cand)):
                            apk_filename = cand
                            break
                    apk_url = f"/api/agents/download/{apk_filename}" if apk_filename else None

                    # Find matching Android Shell bootstrap (.sh)
                    sh_filename = None
                    for cand in [f"install_{app_name}.sh", f"{app_name}.sh", "install.sh"]:
                        if cand in all_files and os.path.isfile(os.path.join(OUTPUT_DIR, cand)):
                            sh_filename = cand
                            break
                    sh_url = f"/api/agents/bootstrap/{sh_filename}" if sh_filename else None

                    one_liner = f"curl -fsSL '{http_url}/api/agents/bootstrap/{sh_filename}' | bash" if sh_filename else None

                    discovered[agent_id] = {
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
                        "zip_filename": filename,
                        "zip_url": f"/api/agents/download/{filename}",
                        "zip_download_url": f"/api/agents/download/{filename}",
                        "apk_filename": apk_filename,
                        "apk_url": apk_url,
                        "apk_download_url": apk_url,
                        "sh_filename": sh_filename,
                        "sh_url": sh_url,
                        "sh_download_url": sh_url,
                        "exe_filename": None,
                        "exe_url": None,
                        "exe_download_url": None,
                        "bat_filename": None,
                        "bat_url": None,
                        "bat_download_url": None,
                        "vbs_filename": None,
                        "vbs_url": None,
                        "vbs_download_url": None,
                        "one_liner": one_liner,
                    }
                    continue

                # Find matching setup exe
                exe_filename = None
                for cand in [f"{app_name}_Setup_{arch}.exe", f"{app_name}.exe", f"TRMM_Agent_Setup_{endpoint_tag}_{arch}.exe"]:
                    if cand in all_files and os.path.isfile(os.path.join(OUTPUT_DIR, cand)):
                        exe_filename = cand
                        break

                # Bootstrap bat
                bat_filename = f"install_{app_name}.bat"
                has_bat = bat_filename in all_files

                # Bootstrap vbs
                vbs_filename = f"install_{app_name}.vbs"
                vbs_path = os.path.join(OUTPUT_DIR, vbs_filename)

                # Auto-generate or upgrade VBS to latest verified syntax
                needs_gen = True
                if os.path.isfile(vbs_path):
                    try:
                        with open(vbs_path, "rb") as vf:
                            vdata = vf.read()
                            if b"WScript.Arguments" in vdata and b"\r\r\n" not in vdata:
                                needs_gen = False
                    except Exception:
                        pass

                if needs_gen:
                    try:
                        vbs_code = generate_vbs_script_content(app_name, app_title, endpoint_tag, agent_id, zip_urls, auto_start)
                        with open(vbs_path, "wb") as vf:
                            vf.write(vbs_code.encode("utf-8"))
                        all_files.add(vbs_filename)
                    except Exception as e:
                        logger.warning(f"Could not generate VBS for {agent_id}: {e}")

                has_vbs = os.path.isfile(vbs_path)

                # One-liner
                bootstrap_urls = [f"{http_url}/api/agents/bootstrap/{bat_filename}"]
                if fallback_base != http_url:
                    bootstrap_urls.append(f"{fallback_base}/api/agents/bootstrap/{bat_filename}")
                bootstrap_urls_ps = ", ".join(f"'{u}'" for u in bootstrap_urls)

                one_liner = (
                    f"$ProgressPreference = 'SilentlyContinue'; "
                    f"[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 -bor [Net.SecurityProtocolType]::Tls13; "
                    f"[System.Net.ServicePointManager]::ServerCertificateValidationCallback = {{$true}}; "
                    f"$b = Join-Path $env:TEMP '{bat_filename}'; "
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
                    f"if (Test-Path $b) {{ & cmd.exe /c $b }} else {{ Write-Error 'Failed to download installer from {http_url}' }}"
                )

                discovered[agent_id] = {
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
                    "zip_filename": filename,
                    "zip_url": f"/api/agents/download/{filename}",
                    "zip_download_url": f"/api/agents/download/{filename}",
                    "exe_filename": exe_filename,
                    "exe_url": f"/api/agents/download/{exe_filename}" if exe_filename else None,
                    "exe_download_url": f"/api/agents/download/{exe_filename}" if exe_filename else None,
                    "bat_filename": bat_filename if has_bat else None,
                    "bat_url": f"/api/agents/bootstrap/{bat_filename}" if has_bat else None,
                    "bat_download_url": f"/api/agents/bootstrap/{bat_filename}" if has_bat else None,
                    "vbs_filename": vbs_filename if has_vbs else None,
                    "vbs_url": f"/api/agents/bootstrap/{vbs_filename}" if has_vbs else None,
                    "vbs_download_url": f"/api/agents/bootstrap/{vbs_filename}" if has_vbs else None,
                    "one_liner": one_liner,
                }
            except Exception as e:
                logger.warning(f"Error inspecting agent package '{filename}': {e}")

        # Merge with existing manifest to preserve custom metadata if any
        for aid, data in discovered.items():
            manifest[aid] = data

        # Remove dead entries
        dead_keys = [aid for aid, data in manifest.items() if aid not in discovered]
        for dk in dead_keys:
            del manifest[dk]

        # Save manifest
        try:
            with open(MANIFEST_FILE, "w", encoding="utf-8") as f:
                json.dump(manifest, f, indent=2)
        except Exception as e:
            logger.warning(f"Could not write {MANIFEST_FILE}: {e}")

        package_list = list(manifest.values())
        package_list.sort(key=lambda p: p.get("mtime", 0), reverse=True)
        return package_list

    @classmethod
    def get_package(cls, agent_id: str) -> Optional[Dict[str, Any]]:
        """Retrieves single package record by agent ID."""
        if not agent_id:
            return None
        aid = str(agent_id).strip()
        if os.path.isfile(MANIFEST_FILE):
            try:
                with open(MANIFEST_FILE, "r", encoding="utf-8") as f:
                    manifest = json.load(f)
                    if aid in manifest:
                        return manifest[aid]
                    for k, v in manifest.items():
                        if k.lower() == aid.lower():
                            return v
            except Exception:
                pass
        for pkg in cls.get_all_packages():
            if pkg.get("agent_id") == aid or str(pkg.get("agent_id", "")).lower() == aid.lower():
                return pkg
        return None

    @staticmethod
    def record_package(pkg_dict: Dict[str, Any]):
        """Records a newly generated package in the manifest."""
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        manifest = {}
        if os.path.isfile(MANIFEST_FILE):
            try:
                with open(MANIFEST_FILE, "r", encoding="utf-8") as f:
                    manifest = json.load(f)
            except Exception:
                pass
        aid = pkg_dict.get("agent_id")
        if aid:
            manifest[aid] = pkg_dict
            try:
                with open(MANIFEST_FILE, "w", encoding="utf-8") as f:
                    json.dump(manifest, f, indent=2)
            except Exception:
                pass

    @staticmethod
    def delete_package(agent_id: str) -> bool:
        """Deletes all package files matching an agent_id."""
        packages = AgentRegistry.get_all_packages()
        pkg = next((p for p in packages if p["agent_id"] == agent_id), None)
        if not pkg:
            return False

        files_to_remove = [
            pkg.get("zip_filename"),
            pkg.get("exe_filename"),
            pkg.get("bat_filename"),
            pkg.get("vbs_filename"),
            pkg.get("apk_filename"),
            pkg.get("sh_filename"),
        ]

        for fname in files_to_remove:
            if fname:
                fpath = os.path.join(OUTPUT_DIR, fname)
                if os.path.isfile(fpath):
                    try:
                        os.remove(fpath)
                    except Exception as e:
                        logger.warning(f"Could not remove {fpath}: {e}")

        # Update manifest
        if os.path.isfile(MANIFEST_FILE):
            try:
                with open(MANIFEST_FILE, "r", encoding="utf-8") as f:
                    manifest = json.load(f)
                if agent_id in manifest:
                    del manifest[agent_id]
                    with open(MANIFEST_FILE, "w", encoding="utf-8") as f:
                        json.dump(manifest, f, indent=2)
            except Exception:
                pass

        return True
