# Utility helpers for AWS EC2 metadata.

"""Provides a simple function to retrieve the instance's public IPv4 address.
"""
import urllib.request

def get_public_ip(timeout: int = 2) -> str | None:
    """Return the public IPv4 address of the current EC2 instance.
    Uses the instance metadata service. Returns ``None`` if the call
    fails (e.g., not running on AWS)."""
    try:
        with urllib.request.urlopen(
            "http://169.254.169.254/latest/meta-data/public-ipv4",
            timeout=timeout,
        ) as resp:
            return resp.read().decode().strip()
    except Exception:
        return None
