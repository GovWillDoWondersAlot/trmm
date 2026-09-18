"""Validate the hub base address used by installers, downloads and agents."""
from urllib.parse import urlsplit, urlunsplit


def normalize_server_url(value):
    if not isinstance(value, str):
        raise ValueError("Server URL must be an HTTP(S) or WS(S) address")
    value = value.strip()
    if any(char.isspace() for char in value) or "\\" in value:
        raise ValueError("Server URL must not contain whitespace or backslashes")
    parsed = urlsplit(value)
    if parsed.scheme not in ("http", "https", "ws", "wss") or not parsed.hostname:
        raise ValueError("Server URL must include http://, https://, ws:// or wss:// and a hostname")
    if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
        raise ValueError("Server URL must not contain credentials, a query or a fragment")
    try:
        parsed.port
    except ValueError as error:
        raise ValueError("Server URL has an invalid port") from error
    path = parsed.path.rstrip("/")
    # Older dashboards supplied these routes instead of a base address.
    while path.endswith("/ws/agent") or path.endswith("/ws"):
        path = path[:-9] if path.endswith("/ws/agent") else path[:-3]
        path = path.rstrip("/")
    if path:
        raise ValueError("Server URL must be the hub base address, without an agent ID or additional path")
    scheme = {"http": "ws", "https": "wss"}.get(parsed.scheme, parsed.scheme)
    return urlunsplit((scheme, parsed.netloc, "", "", ""))


def http_server_url(value):
    parsed = urlsplit(normalize_server_url(value))
    return urlunsplit(("https" if parsed.scheme == "wss" else "http", parsed.netloc, "", "", ""))
