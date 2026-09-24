"""Package boundary, not a working agent installer.

The former implementation copied arbitrary bytes and reported success. Until a
reviewed per-user package/transaction contract exists, fail without modifying the
machine. In particular, never delete an existing TRMM installation as 'rollback'.
"""
import json
from pathlib import Path


class PerUserInstaller:
    def __init__(self, spec_path=None):
        spec = Path(spec_path) if spec_path else Path(__file__).with_name('per_user_spec.json')
        self.spec = json.loads(spec.read_text(encoding='utf-8'))

    def check_capabilities(self, requested_features=None):
        requested = set(requested_features or ())
        unsupported = requested & set(self.spec.get('admin_required_features', ()))
        if unsupported:
            return 'ADMIN_REQUIRED', 'Requested functionality requires an approved administrative installation.'
        if requested:
            return 'FAILED', 'Requested capabilities have not been verified for per-user installation.'
        return 'OK', None

    def install(self, source_payload_path, enable_startup=False, requested_features=None):
        status, reason = self.check_capabilities(requested_features)
        if status != 'OK':
            return {'status': status, 'reason': reason}
        return {'status': 'FAILED', 'error_code': 'PACKAGE_NOT_READY',
                'reason': 'No verified per-user package is available. No files or startup settings were changed.'}

    def uninstall(self, remove_logs=True):
        return {'status': 'FAILED', 'error_code': 'NO_MANAGED_INSTALLATION',
                'reason': 'No installation ownership record is available. Use the existing product uninstaller.'}
