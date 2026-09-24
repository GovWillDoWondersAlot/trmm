"""Non-distributable protocol harness. No agent payload is downloaded or executed.

Preserves testable consent and reporting semantics without pretending to be a
native installer. Production distribution remains blocked until the real release
artifacts and authenticated agent-enrollment handshake have been verified.
"""
import json
import os
from pathlib import Path
import sys
import urllib.request
from urllib.parse import urlsplit
import uuid


class ProtocolHarness:
    def __init__(self, hub_url, invitation_code, auto_consent=False, queue_path=None):
        origin = urlsplit(hub_url)
        if origin.scheme != 'https' or not origin.hostname or origin.username or origin.password or origin.query or origin.fragment or origin.path not in ('', '/'):
            raise ValueError('A trusted HTTPS hub origin is required')
        if auto_consent:
            raise ValueError('Automatic consent is not supported for website distribution')
        self.hub_url = hub_url.rstrip('/')
        self.invitation_code = invitation_code
        self.attempt_id = self.report_token = None
        self.seq = 0
        self.pending = []
        self.queue_path = Path(queue_path) if queue_path else None
        if self.queue_path and self.queue_path.exists():
            raw = self.queue_path.read_bytes()
            if len(raw) > 1024 * 1024:
                raise ValueError('Telemetry queue exceeds size limit')
            saved = json.loads(raw)
            self.attempt_id = saved['attempt_id']
            self.seq = saved['seq']
            self.pending = saved['pending']
            # Credentials must be provided again; never persist them in this file.

    def _http_post(self, path, payload):
        req = urllib.request.Request(self.hub_url + path, data=json.dumps(payload).encode(),
                                     headers={'Content-Type': 'application/json'})
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                raise ValueError('Redirect refused for credential-bearing report')
        with urllib.request.build_opener(NoRedirect).open(req, timeout=10) as response:
            raw = response.read(16385)
            if len(raw) > 16384:
                raise ValueError('Oversized response')
            return json.loads(raw)

    def _save_queue(self):
        if self.queue_path:
            self.queue_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.queue_path.with_name(self.queue_path.name + '.partial')
            raw = json.dumps({'attempt_id': self.attempt_id, 'seq': self.seq, 'pending': self.pending})
            if len(raw.encode()) > 1024 * 1024:
                raise ValueError('Telemetry queue exceeds size limit')
            temporary.write_text(raw, encoding='utf-8')
            os.replace(temporary, self.queue_path)

    def flush_reports(self):
        if not self.report_token:
            return False
        while self.pending:
            event = self.pending[0]
            try:
                response = self._http_post('/api/installations/report',
                                           {**event, 'report_token': self.report_token})
                if response.get('status') != 'ACCEPTED':
                    return False
            except (OSError, ValueError):
                return False
            self.pending.pop(0)
            self._save_queue()
        return True

    def report_stage(self, stage, payload=None):
        if not self.attempt_id or not self.report_token:
            return False
        if len(self.pending) >= 256:
            return False
        self.seq += 1
        self.pending.append({'attempt_id': self.attempt_id, 'seq': self.seq,
                             'idempotency_key': str(uuid.uuid4()), 'stage': stage,
                             'payload': payload or {}})
        self._save_queue()
        return self.flush_reports()

    def prompt_user_consent(self):
        if not sys.stdin or not sys.stdin.isatty():
            return False
        try:
            return input('Approve installing TRMM Remote Support on this computer? [y/N]: ').strip().lower() in ('y', 'yes')
        except (EOFError, OSError):
            return False

    def run(self, requested_features=None):
        # Do not consume an invitation, touch an install, or download a fixture.
        return {'status': 'FAILED', 'error_code': 'RELEASE_NOT_READY',
                'reason': 'This Python protocol harness is not a released Windows installer.'}


# Compatibility for old test imports; not a claim of a native artifact.
NativeBootstrapper = ProtocolHarness

if __name__ == '__main__':
    raise SystemExit('RELEASE_NOT_READY: this protocol harness is not a Windows installer.')
