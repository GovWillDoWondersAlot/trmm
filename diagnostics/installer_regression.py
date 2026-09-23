"""Installer/configuration regressions with isolated payloads; no agent executes."""
import importlib.util
import json
import errno
from pathlib import Path
import shutil
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile
import uuid
from contextlib import contextmanager

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server_address import normalize_server_url, http_server_url

TEMP_BASE = ROOT / 'diagnostics' / 'mirror_live' / 'installer-tests'
TEMP_BASE.mkdir(parents=True, exist_ok=True)


@contextmanager
def temporary_directory():
    path = TEMP_BASE / uuid.uuid4().hex
    path.mkdir()
    try:
        yield str(path)
    finally:
        assert path.resolve().parent == TEMP_BASE.resolve()
        shutil.rmtree(path)


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


package = ModuleType('installer_test_package')
package.__path__ = []
sys.modules[package.__name__] = package
compiler = load('installer_test_package.setup_compiler', 'master_hub/setup_compiler.py')
generator = load('installer_test_package.generator', 'master_hub/generator.py')


class InstallerTests(unittest.TestCase):
    def setUp(self):
        temp_patch = patch.object(compiler.tempfile, 'gettempdir', return_value=str(TEMP_BASE))
        temp_patch.start()
        self.addCleanup(temp_patch.stop)

    def test_address_normalization(self):
        for address in ('https://hub.swiftvtu.com', 'wss://hub.swiftvtu.com/ws/agent/',
                        'wss://hub.swiftvtu.com/ws/ws/agent', 'wss://hub.swiftvtu.com/ws/agent/ws/agent'):
            self.assertEqual(normalize_server_url(address), 'wss://hub.swiftvtu.com')
            self.assertEqual(http_server_url(address), 'https://hub.swiftvtu.com')
        self.assertEqual(normalize_server_url('http://[::1]:8000/ws'), 'ws://[::1]:8000')
        self.assertEqual(normalize_server_url('ws://192.168.1.125:8000'), 'ws://192.168.1.125:8000')

    def test_reject_invalid_addresses(self):
        for value in ('', 'hub.test', 'ftp://hub.test', 'ws://u:p@hub.test', 'ws://hub.test?token=1',
                      'ws://hub.test#frag', 'ws://hub.test/ws/agent/device', 'ws://hub.test:bad'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_server_url(value)

    def test_package_uses_base_url_and_reports_compiler_failure(self):
        with temporary_directory() as tmp:
            root = Path(tmp)
            dist, output = root / 'dist', root / 'output'
            dist.mkdir(); output.mkdir()
            (root / 'hvnc').mkdir()  # Empty payload placeholders; no desktop implementation is loaded.
            (root / 'agent_client').mkdir()
            (dist / 'TRMM_Agent.exe').write_bytes(b'fixture executable: never run')
            shutil.copyfile(ROOT / 'server_address.py', root / 'server_address.py')
            shutil.copyfile(ROOT / 'device_identity.py', root / 'device_identity.py')
            with patch.multiple(generator, ROOT_DIR=str(root), DIST_AGENT_DIR=str(dist), OUTPUT_DIR=str(output)), \
                 patch.object(generator.SetupCompiler, 'compile_installer', side_effect=RuntimeError('Fixture compiler failure')):
                result = generator.AgentGenerator.build_package({'agent_id':'test','endpoint_tag':'ConnectionAudit',
                    'server_url':'wss://hub.swiftvtu.com/ws/agent','auto_start':False})
            self.assertFalse(result['installer_ready'])
            self.assertEqual(result['installer_error'], 'Fixture compiler failure')
            self.assertIsNone(result['exe_filename'])
            self.assertIn('https://hub.swiftvtu.com/api/agents/bootstrap/', result['one_liner'])
            self.assertNotIn('/ws/agent/api/', result['one_liner'])
            self.assertNotIn('%TEMP%', result['one_liner'])
            self.assertIn('$env:TEMP', result['one_liner'])
            with zipfile.ZipFile(result['zip_path']) as archive:
                self.assertEqual(json.loads(archive.read('config.json'))['server_url'], 'wss://hub.swiftvtu.com')
                self.assertIn('server_address.py', archive.namelist())
                self.assertIn('device_identity.py', archive.namelist())
            self.assertIn('https://hub.swiftvtu.com/api/agents/download/', Path(result['bootstrap_path']).read_text())

    def test_missing_runtime_fails_before_generating_installers(self):
        with temporary_directory() as tmp, patch.object(generator, 'DIST_AGENT_DIR', tmp):
            with self.assertRaisesRegex(ValueError, 'Native agent runtime is missing'):
                generator.AgentGenerator.build_package({'server_url':'https://hub.swiftvtu.com'})

    def test_linux_compiler_failure_does_not_try_pyinstaller(self):
        with temporary_directory() as tmp:
            root = Path(tmp)
            payload = root / 'payload.zip'
            with zipfile.ZipFile(payload, 'w') as archive: archive.writestr('config.json', '{}')
            with patch.object(compiler, 'OUTPUT_DIR', tmp), \
                 patch.object(compiler, 'sys', SimpleNamespace(platform='linux', executable='python')), \
                 patch.object(compiler.shutil, 'which', return_value='/usr/bin/makensis'), \
                 patch.object(compiler.SetupCompiler, '_compile_nsis', side_effect=RuntimeError('NSIS fwrite failed')), \
                 patch.object(compiler.subprocess, 'run') as run:
                with self.assertRaisesRegex(RuntimeError, 'NSIS fwrite failed'):
                    compiler.SetupCompiler.compile_installer(str(payload), 'audit-regression', 'Audit')
                run.assert_not_called()

    def test_low_disk_space_is_reported_before_compilation(self):
        with temporary_directory() as tmp:
            payload = Path(tmp) / 'payload.zip'
            with zipfile.ZipFile(payload, 'w') as archive: archive.writestr('config.json', '{}')
            with patch.object(compiler, 'OUTPUT_DIR', tmp), \
                 patch.object(compiler.shutil, 'disk_usage', return_value=SimpleNamespace(free=1)), \
                 patch.object(compiler.subprocess, 'run') as run:
                with self.assertRaisesRegex(RuntimeError, 'Insufficient temporary disk space'):
                    compiler.SetupCompiler.compile_installer(str(payload), 'audit-disk-regression', 'Audit')
                run.assert_not_called()

    def test_low_disk_space_rejected_before_runtime_copy(self):
        with temporary_directory() as tmp, patch.object(generator, 'OUTPUT_DIR', tmp), \
             patch.object(generator.shutil, 'disk_usage', return_value=SimpleNamespace(free=0)), \
             patch.object(generator.AgentGenerator, '_build_package') as build:
            with self.assertRaisesRegex(generator.BuildStorageError, 'Not enough server disk space'):
                generator.AgentGenerator.build_package({'agent_id':'low-space'})
            build.assert_not_called()
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_copy_failure_cleans_up_abandoned_build(self):
        with temporary_directory() as tmp:
            def failing_build(config):
                build_dir = Path(tmp) / ('build_' + config['agent_id'])
                build_dir.mkdir()
                (build_dir / 'partial-runtime').write_bytes(b'partial')
                raise OSError(errno.ENOSPC, 'No space left on device')
            with patch.object(generator, 'OUTPUT_DIR', tmp), \
                 patch.object(generator, 'require_build_space'), \
                 patch.object(generator.AgentGenerator, '_build_package', side_effect=failing_build):
                with self.assertRaisesRegex(generator.BuildStorageError, 'ran out of disk space'):
                    generator.AgentGenerator.build_package({'agent_id':'copy-failure'})
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_zip_failure_removes_partial_download(self):
        with temporary_directory() as tmp:
            root = Path(tmp)
            for name in ('dist', 'output', 'hvnc', 'agent_client'):
                (root / name).mkdir()
            (root / 'dist' / 'TRMM_Agent.exe').write_bytes(b'fixture')
            with patch.multiple(generator, ROOT_DIR=tmp, DIST_AGENT_DIR=str(root/'dist'), OUTPUT_DIR=str(root/'output')), \
                 patch.object(generator.zipfile.ZipFile, 'write', side_effect=OSError(errno.ENOSPC, 'disk full')):
                with self.assertRaises(generator.BuildStorageError):
                    generator.AgentGenerator.build_package({'agent_id':'zip-failure','server_url':'https://hub.swiftvtu.com'})
            self.assertEqual(list((root/'output').iterdir()), [])

    def test_failed_rebuild_preserves_previous_zip(self):
        with temporary_directory() as tmp:
            root = Path(tmp)
            for name in ('dist', 'output', 'hvnc', 'agent_client'):
                (root / name).mkdir()
            (root / 'dist' / 'TRMM_Agent.exe').write_bytes(b'fixture')
            previous = root / 'output' / 'TestAgent_Fixture_x64.zip'
            with zipfile.ZipFile(previous, 'w') as archive:
                archive.writestr('config.json', '{}')
            saved = previous.read_bytes()
            with patch.multiple(generator, ROOT_DIR=tmp, DIST_AGENT_DIR=str(root/'dist'), OUTPUT_DIR=str(root/'output')), \
                 patch.object(generator.zipfile.ZipFile, 'write', side_effect=OSError(errno.ENOSPC, 'disk full')):
                with self.assertRaises(generator.BuildStorageError):
                    generator.AgentGenerator.build_package({'agent_id':'rebuild','endpoint_tag':'Fixture',
                        'custom_name':'TestAgent','server_url':'https://hub.swiftvtu.com'})
            self.assertEqual(previous.read_bytes(), saved)
            self.assertEqual(list((root/'output').iterdir()), [previous])


if __name__ == '__main__':
    unittest.main()
