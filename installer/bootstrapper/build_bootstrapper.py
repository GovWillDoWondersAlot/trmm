"""Validate a real release artifact; never relabel a Python archive as an EXE."""
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
    raise RuntimeError('NATIVE_BUILD_NOT_IMPLEMENTED: no standalone Windows downloader has been built. '
                       'The old .pyz artifact is not distributable. Native build and clean-Windows validation are required.')


if __name__ == '__main__':
    raise SystemExit(str(RuntimeError('NATIVE_BUILD_NOT_IMPLEMENTED: see diagnostics/INSTALLATION_CORRECTION_STATUS.md')))
