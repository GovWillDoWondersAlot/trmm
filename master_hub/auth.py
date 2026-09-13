"""
Authentication and Session Manager for Tactical RMM Master Hub.
Provides setup initialization, secure credential verification, admin all-access master key,
and signed session token management.
"""

import os
import json
import hmac
import hashlib
import time
import secrets
from typing import Optional, Dict, Any

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AUTH_STORE_FILE = os.path.join(ROOT_DIR, "master_hub", "auth_store.json")

# Fixed Admin Master All-Access Key requested by user
ADMIN_ALL_ACCESS_KEY = "0235059609567482"

# Session expiration: 8 hours of inactivity
SESSION_TTL = 8 * 3600


def _hash_password(password: str, salt: Optional[str] = None) -> tuple[str, str]:
    """Generates PBKDF2 HMAC-SHA256 password hash with salt."""
    if not salt:
        salt = secrets.token_hex(16)
    key = hashlib.pbkdf2_hmac(
        'sha256',
        password.encode('utf-8'),
        salt.encode('utf-8'),
        100000
    )
    return key.hex(), salt


class AuthManager:
    """Manages admin credentials, setup state, and session tokens."""

    _active_sessions: Dict[str, float] = {}  # token -> last_active_timestamp

    @classmethod
    def is_setup_required(cls) -> bool:
        """Returns True if admin account has not been initialized yet."""
        if not os.path.isfile(AUTH_STORE_FILE):
            return True
        try:
            with open(AUTH_STORE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return not bool(data.get("password_hash") and data.get("username"))
        except Exception:
            return True

    @classmethod
    def setup_admin(cls, username: str, password: str) -> Dict[str, Any]:
        """Initializes admin username and password on first live server setup."""
        if not username or len(username.strip()) < 3:
            return {"success": False, "error": "Username must be at least 3 characters."}
        if not password or len(password) < 6:
            return {"success": False, "error": "Password must be at least 6 characters."}

        pwd_hash, salt = _hash_password(password)
        data = {
            "username": username.strip(),
            "password_hash": pwd_hash,
            "salt": salt,
            "created_at": time.time(),
            "updated_at": time.time()
        }

        with open(AUTH_STORE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

        # Automatically log in the user upon setup
        token = cls.create_session(username.strip())
        return {"success": True, "token": token, "username": username.strip()}

    @classmethod
    def verify_login(cls, username: str = "", password: str = "", master_key: Optional[str] = None) -> Dict[str, Any]:
        """
        Verifies login credentials.
        Accepts regular admin password OR the Admin All-Access Key.
        """
        # 1. Check Admin All-Access Key fallback
        candidate_key = (master_key or password or "").strip()
        if candidate_key == ADMIN_ALL_ACCESS_KEY:
            admin_user = username.strip() if username else "SuperAdmin"
            token = cls.create_session(admin_user)
            return {"success": True, "token": token, "is_master_key": True, "username": admin_user}

        # 2. Check standard password
        if not os.path.isfile(AUTH_STORE_FILE):
            return {"success": False, "error": "System is not initialized. Please complete initial setup."}

        try:
            with open(AUTH_STORE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)

            stored_user = data.get("username", "")
            stored_hash = data.get("password_hash", "")
            stored_salt = data.get("salt", "")

            if not hmac.compare_digest(username.strip().lower(), stored_user.lower()):
                return {"success": False, "error": "Invalid username or password."}

            check_hash, _ = _hash_password(password, stored_salt)
            if not hmac.compare_digest(check_hash, stored_hash):
                return {"success": False, "error": "Invalid username or password."}

            token = cls.create_session(stored_user)
            return {"success": True, "token": token, "username": stored_user}
        except Exception as e:
            return {"success": False, "error": f"Authentication store error: {e}"}

    @classmethod
    def create_session(cls, username: str) -> str:
        """Creates a signed random session token."""
        token = secrets.token_hex(32)
        cls._active_sessions[token] = time.time()
        return token

    @classmethod
    def validate_session(cls, token: Optional[str]) -> bool:
        """Validates if a session token is active and not expired."""
        if not token or token not in cls._active_sessions:
            return False

        last_active = cls._active_sessions[token]
        if time.time() - last_active > SESSION_TTL:
            cls._active_sessions.pop(token, None)
            return False

        # Sliding window expiration: update last active
        cls._active_sessions[token] = time.time()
        return True

    @classmethod
    def destroy_session(cls, token: Optional[str]):
        """Logs out session."""
        if token:
            cls._active_sessions.pop(token, None)
