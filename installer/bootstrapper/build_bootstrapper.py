import os
import sys
import subprocess
from pathlib import Path
import struct

def validate_windows_executable(path):
    path = Path(path)
    data = path.read_bytes()
    if path.suffix.lower() != '.exe' or len(data) < 64 or data[:2] != b'MZ':
        raise ValueError('A genuine Windows PE .exe is required, not a Python archive')
    offset = struct.unpack_from('<I', data, 0x3c)[0]
    if offset < 64 or offset + 24 > len(data) or data[offset:offset + 4] != b'PE\0\0':
        raise ValueError('Invalid Windows executable header')
    machine, sections = struct.unpack_from('<HH', data, offset + 4)
    if machine not in (0x8664, 0xAA64) or sections == 0:
        raise ValueError('Unsupported or invalid Windows executable')
    if len(data) > 1_000_000:
        raise ValueError('Downloader exceeds 1,000,000 bytes')
    return len(data)

def build_standalone_bootstrapper():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    dist_dir = os.path.join(base_dir, "dist")
    os.makedirs(dist_dir, exist_ok=True)
    
    cs_src = os.path.join(base_dir, "trmm_web_installer.cs")
    out_exe = os.path.join(dist_dir, "trmm_web_installer.exe")
    csc_path = r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe"

    if not os.path.isfile(csc_path):
        raise RuntimeError(f"C# Compiler not found at {csc_path}")

    cmd = [
        csc_path,
        "/target:winexe",
        "/platform:x64",
        f"/out:{out_exe}",
        "/r:System.Windows.Forms.dll",
        "/r:System.dll",
        "/r:System.Drawing.dll",
        cs_src
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"Native compilation failed: {res.stderr}")

    size = validate_windows_executable(out_exe)
    print(f"Native Windows Bootstrapper EXE compiled successfully: {out_exe}")
    print(f"Measured PE size: {size} bytes ({round(size/1024, 2)} KB / {round(size/1048576, 4)} MB)")
    return out_exe, size

if __name__ == "__main__":
    build_standalone_bootstrapper()
