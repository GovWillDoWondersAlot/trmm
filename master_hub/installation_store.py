import os
import sqlite3
import json
import hashlib
import uuid
import secrets
import hmac
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta

class InstallationStore:
    """
    Persistent store using SQLite for TRMM web installer invitations, attempt tracking, and progress telemetry events.
    """
    VALID_STAGES = [
        "AWAITING_START", "STARTED", "CONSENTED", "CHECKING",
        "DOWNLOADING", "VERIFYING", "INSTALLING", "INSTALLED",
        "CONNECTING", "ONLINE", "FAILED", "CANCELLED",
        "ADMIN_REQUIRED", "REBOOT_REQUIRED"
    ]
    PROGRESS_STAGES = VALID_STAGES[1:9]
    TERMINAL_STAGES = {'ONLINE', 'FAILED', 'CANCELLED', 'ADMIN_REQUIRED', 'REBOOT_REQUIRED'}

    def __init__(self, db_path=None):
        if db_path is None:
            base_dir = os.path.dirname(os.path.abspath(__file__))
            db_path = os.path.join(base_dir, "trmm_installations.db")

        self.db_path = db_path
        self._init_sqlite()

    @contextmanager
    def _get_connection(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute('PRAGMA foreign_keys=ON')
            # Serialize read/check/write transactions, including single-use redemption.
            conn.execute('BEGIN IMMEDIATE')
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_sqlite(self):
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS invitations (
                id TEXT PRIMARY KEY,
                code_hash TEXT UNIQUE NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT,
                max_uses INTEGER DEFAULT 1,
                use_count INTEGER DEFAULT 0,
                revoked INTEGER DEFAULT 0
            )
            """)

            cursor.execute("""
            CREATE TABLE IF NOT EXISTS attempts (
                id TEXT PRIMARY KEY,
                invitation_id TEXT NOT NULL,
                report_token_hash TEXT NOT NULL,
                device_id TEXT,
                release_version TEXT,
                architecture TEXT,
                current_stage TEXT NOT NULL DEFAULT 'AWAITING_START',
                download_bytes INTEGER DEFAULT 0,
                total_bytes INTEGER DEFAULT 0,
                retry_count INTEGER DEFAULT 0,
                exit_code INTEGER,
                error_code TEXT,
                error_detail TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (invitation_id) REFERENCES invitations (id)
            )
            """)

            cursor.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id TEXT PRIMARY KEY,
                attempt_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                idempotency_key TEXT UNIQUE NOT NULL,
                stage TEXT NOT NULL,
                payload_json TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (attempt_id) REFERENCES attempts (id)
            )
            """)
            columns = {r['name'] for r in conn.execute('PRAGMA table_info(attempts)')}
            for name, definition in [('last_seq', 'INTEGER NOT NULL DEFAULT 0'),
                                     ('token_expires_at', "TEXT NOT NULL DEFAULT ''")]:
                if name not in columns:
                    conn.execute(f'ALTER TABLE attempts ADD COLUMN {name} {definition}')
            # Existing demo credentials have no expiry or trustworthy history.
            conn.execute("UPDATE attempts SET current_stage='FAILED', error_code='LEGACY_UNVERIFIED' WHERE token_expires_at='' AND error_code IS NOT 'LEGACY_UNVERIFIED'")
            conn.execute('PRAGMA user_version=2')

    @staticmethod
    def _hash_secret(secret):
        return hashlib.sha256(secret.encode("utf-8")).hexdigest()

    def create_invitation(self, code, expires_at=None, max_uses=1):
        if not isinstance(code, str) or not 8 <= len(code) <= 128:
            raise ValueError('Invalid invitation code')
        if type(max_uses) is not int or not 1 <= max_uses <= 10:
            raise ValueError('Invalid use limit')
        created = datetime.now(timezone.utc)
        expiry = datetime.fromisoformat(expires_at) if expires_at else created + timedelta(hours=1)
        if expiry.tzinfo is None or not created < expiry <= created + timedelta(hours=24):
            raise ValueError('Expiry must be within 24 hours and include timezone')
        expires_at = expiry.astimezone(timezone.utc).isoformat()
        inv_id = str(uuid.uuid4())
        code_hash = self._hash_secret(code)
        now = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            conn.cursor().execute(
                "INSERT INTO invitations (id, code_hash, created_at, expires_at, max_uses, use_count, revoked) VALUES (?, ?, ?, ?, ?, 0, 0)",
                (inv_id, code_hash, now, expires_at, max_uses)
            )
            conn.commit()
        return inv_id

    def revoke_invitation(self, inv_id):
        with self._get_connection() as conn:
            conn.cursor().execute("UPDATE invitations SET revoked = 1 WHERE id = ?", (inv_id,))
            conn.commit()

    def redeem_invitation(self, code, architecture="x64", release_version="1.0.0"):
        if not isinstance(code, str) or not 8 <= len(code) <= 128:
            return None, 'INVALID_CODE'
        if architecture not in ('x64', 'arm64') or not isinstance(release_version, str) or not 1 <= len(release_version) <= 64:
            return None, 'INVALID_RELEASE'
        code_hash = self._hash_secret(code)
        now = datetime.now(timezone.utc).isoformat()

        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM invitations WHERE code_hash = ?", (code_hash,))
            inv = cursor.fetchone()

            if not inv:
                return None, "INVALID_CODE"
            if inv["revoked"]:
                return None, "INVITATION_REVOKED"
            if not inv["expires_at"] or inv["expires_at"] < now:
                return None, "INVITATION_EXPIRED"
            if inv["max_uses"] < 1 or inv["use_count"] >= inv["max_uses"]:
                return None, "INVITATION_EXHAUSTED"

            # Increment use count
            cursor.execute("UPDATE invitations SET use_count = use_count + 1 WHERE id = ?", (inv["id"],))

            attempt_id = str(uuid.uuid4())
            report_token = secrets.token_urlsafe(32)
            token_hash = self._hash_secret(report_token)

            cursor.execute(
                """INSERT INTO attempts 
                   (id, invitation_id, report_token_hash, release_version, architecture, current_stage, created_at, updated_at, token_expires_at) 
                   VALUES (?, ?, ?, ?, ?, 'STARTED', ?, ?, ?)""",
                (attempt_id, inv["id"], token_hash, release_version, architecture, now, now,
                 (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat())
            )
            conn.commit()

        return {"attempt_id": attempt_id, "report_token": report_token}, None

    def verify_report_token(self, attempt_id, report_token):
        if not isinstance(report_token, str) or len(report_token) > 256:
            return False
        token_hash = self._hash_secret(report_token)
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT a.id FROM attempts a JOIN invitations i ON a.invitation_id=i.id WHERE a.id=? AND a.report_token_hash=? AND a.token_expires_at>? AND i.revoked=0", (attempt_id, token_hash, datetime.now(timezone.utc).isoformat()))
            return cursor.fetchone() is not None

    def record_event(self, attempt_id, report_token, seq, idempotency_key, stage, payload=None):
        if not self.verify_report_token(attempt_id, report_token):
            return False, "UNAUTHORIZED_ATTEMPT_TOKEN"

        if stage not in self.VALID_STAGES:
            return False, f"INVALID_STAGE: {stage}"
        if stage in ('ONLINE', 'AWAITING_START'):
            return False, 'SERVER_ONLY_STAGE'
        if type(seq) is not int or not 1 <= seq <= 1000000:
            return False, 'INVALID_SEQUENCE'
        if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 128:
            return False, 'INVALID_IDEMPOTENCY_KEY'

        if payload is None:
            payload = {}
        allowed = {'downloaded_bytes', 'total_bytes', 'retry_count', 'exit_code', 'error_code', 'error_detail'}
        if not isinstance(payload, dict) or payload.keys() - allowed:
            return False, 'INVALID_PAYLOAD'
        for key in ('downloaded_bytes', 'total_bytes', 'retry_count'):
            if key in payload and (type(payload[key]) is not int or not 0 <= payload[key] <= 10**12):
                return False, 'INVALID_PAYLOAD'
        if 'exit_code' in payload and (type(payload['exit_code']) is not int or abs(payload['exit_code']) > 2**32):
            return False, 'INVALID_PAYLOAD'
        for key in ('error_code', 'error_detail'):
            if key in payload and (not isinstance(payload[key], str) or len(payload[key]) > (64 if key == 'error_code' else 1024)):
                return False, 'INVALID_PAYLOAD'

        now = datetime.now(timezone.utc).isoformat()
        payload_str = json.dumps(payload, sort_keys=True, separators=(',', ':'), allow_nan=False)
        event_id = str(uuid.uuid4())

        with self._get_connection() as conn:
            cursor = conn.cursor()
            # Recheck authorization under the same write transaction as the event.
            cursor.execute('SELECT a.*, i.revoked FROM attempts a JOIN invitations i ON a.invitation_id=i.id WHERE a.id=?', (attempt_id,))
            attempt = cursor.fetchone()
            if not attempt or attempt['revoked'] or attempt['token_expires_at'] <= now or not hmac.compare_digest(attempt['report_token_hash'], self._hash_secret(report_token)):
                return False, 'UNAUTHORIZED_ATTEMPT_TOKEN'
            # Namespace external keys by attempt while retaining the legacy table.
            idempotency_key = attempt_id + ':' + idempotency_key
            # Check idempotency
            cursor.execute("SELECT * FROM events WHERE idempotency_key = ? OR (attempt_id=? AND seq=?)", (idempotency_key, attempt_id, seq))
            prior = cursor.fetchone()
            if prior:
                if (prior['attempt_id'], prior['seq'], prior['idempotency_key'], prior['stage'], prior['payload_json']) == (attempt_id, seq, idempotency_key, stage, payload_str):
                    return True, 'IDEMPOTENT_IGNORE'
                return False, 'EVENT_CONFLICT'
            if seq <= attempt['last_seq']:
                return False, 'STALE_EVENT'
            if seq != attempt['last_seq'] + 1:
                return False, 'SEQUENCE_GAP'
            if attempt['current_stage'] in self.TERMINAL_STAGES:
                return False, 'TERMINAL_ATTEMPT'
            if stage not in self.TERMINAL_STAGES:
                old = self.PROGRESS_STAGES.index(attempt['current_stage'])
                if self.PROGRESS_STAGES.index(stage) not in (old, old + 1):
                    return False, 'INVALID_TRANSITION'
            downloaded = payload.get('downloaded_bytes', attempt['download_bytes'])
            total = payload.get('total_bytes', attempt['total_bytes'])
            if downloaded < attempt['download_bytes'] or (total and downloaded > total) or (attempt['total_bytes'] and total != attempt['total_bytes']):
                return False, 'INVALID_PROGRESS'

            try:
                cursor.execute(
                    "INSERT INTO events (id, attempt_id, seq, idempotency_key, stage, payload_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (event_id, attempt_id, seq, idempotency_key, stage, payload_str, now)
                )

                # Update attempt state
                download_bytes = payload.get("downloaded_bytes", 0)
                total_bytes = payload.get("total_bytes", 0)
                device_id = payload.get("device_id")
                exit_code = payload.get("exit_code")
                error_code = payload.get("error_code")
                error_detail = payload.get("error_detail")
                retry_count = payload.get("retry_count", 0)

                cursor.execute("""
                    UPDATE attempts SET 
                        current_stage = ?,
                        last_seq = ?,
                        updated_at = ?,
                        download_bytes = MAX(download_bytes, ?),
                        total_bytes = MAX(total_bytes, ?),
                        device_id = COALESCE(?, device_id),
                        exit_code = COALESCE(?, exit_code),
                        error_code = COALESCE(?, error_code),
                        error_detail = COALESCE(?, error_detail),
                        retry_count = MAX(retry_count, ?)
                    WHERE id = ?
                """, (stage, seq, now, download_bytes, total_bytes, device_id, exit_code, error_code, error_detail, retry_count, attempt_id))

                conn.commit()
                return True, "RECORDED"
            except sqlite3.IntegrityError as e:
                return False, 'EVENT_CONFLICT'

    def list_invitations(self):
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id, created_at, expires_at, max_uses, use_count, revoked FROM invitations ORDER BY created_at DESC LIMIT 200")
            return [dict(r) for r in cursor.fetchall()]

    def list_attempts(self):
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT a.id, a.invitation_id, a.device_id, a.release_version, a.architecture, 
                       a.current_stage, a.download_bytes, a.total_bytes, a.retry_count, 
                       a.exit_code, a.error_code, a.error_detail, a.created_at, a.updated_at
                FROM attempts a
                ORDER BY a.updated_at DESC LIMIT 200
            """)
            rows = [dict(r) for r in cursor.fetchall()]
            for row in rows:
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(row['updated_at'])).total_seconds()
                row['connection_status'] = 'STATUS_UNKNOWN' if age > 30 and row['current_stage'] not in self.TERMINAL_STAGES else 'REPORTED'
            return rows

    def get_attempt_events(self, attempt_id):
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT seq, stage, payload_json, created_at FROM events WHERE attempt_id = ? ORDER BY seq DESC LIMIT 500", (attempt_id,))
            events = []
            for r in cursor.fetchall():
                d = dict(r)
                if d["payload_json"]:
                    d["payload"] = json.loads(d["payload_json"])
                else:
                    d["payload"] = {}
                del d["payload_json"]
                events.append(d)
            return list(reversed(events))
