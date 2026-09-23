"""Machine identity is independent of which installer enrolled the endpoint."""
from functools import lru_cache
import hashlib
import socket
import uuid


def identity_from_machine_guid(value):
    guid = uuid.UUID(str(value).strip())
    if guid.int == 0:
        raise ValueError("Empty Windows machine identity")
    return hashlib.sha256(('trmm-device-v1:' + str(guid)).encode()).hexdigest()[:32]


@lru_cache(maxsize=1)
def get_device_id():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\Microsoft\Cryptography',
                            0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
            value, _ = winreg.QueryValueEx(key, 'MachineGuid')
        return identity_from_machine_guid(value)
    except (ImportError, OSError, ValueError):
        # Fallback for environments without the Windows installation identifier.
        seed = f'trmm-fallback-v1:{socket.gethostname().lower()}:{uuid.getnode():012x}'
        return hashlib.sha256(seed.encode()).hexdigest()[:32]
