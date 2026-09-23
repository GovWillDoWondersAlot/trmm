"""Identity and curtain checks with no agent, registry writes, or hardware access."""
import ast
import ctypes.wintypes
import logging
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import device_identity


def method(path, class_name, method_name):
    tree = ast.parse((ROOT / path).read_text(encoding='utf-8'))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name)
    return next(n for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == method_name)


class DeviceCurtainTests(unittest.TestCase):
    def test_machine_identity_is_independent_of_installer(self):
        guid = '01234567-89ab-cdef-0123-456789abcdef'
        stable = device_identity.identity_from_machine_guid(guid)
        self.assertEqual(stable, device_identity.identity_from_machine_guid(guid.upper()))
        self.assertNotEqual(stable, device_identity.identity_from_machine_guid('11234567-89ab-cdef-0123-456789abcdef'))
        constructor = method('agent_client/client.py', 'AgentClient', '__init__')
        # Only the identity initialization, before any Windows/desktop setup.
        identity_body = constructor.body[:6]
        with patch.object(device_identity, 'get_device_id', return_value=stable):
            for installer_id in ('first-installer', 'second-installer'):
                original = {'agent_id': installer_id}
                obj = SimpleNamespace()
                ns = {'self': obj, 'config': original}
                exec(compile(ast.Module(body=identity_body, type_ignores=[]), '<identity>', 'exec'), ns)
                self.assertEqual(obj.agent_id, stable)
                self.assertEqual(obj.installation_id, installer_id)
                self.assertEqual(original['agent_id'], installer_id)

    def test_stale_disconnect_does_not_mark_replacement_offline(self):
        fn = method('master_hub/agent_manager.py', 'AgentManager', 'unregister_connection')
        fn.args.args = [ast.arg(arg='self'), ast.arg(arg='agent_id'), ast.arg(arg='ws')]
        fn.returns = None
        ns = {'logger': logging.getLogger('test')}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[])), '<disconnect>', 'exec'), ns)
        obj = SimpleNamespace(active_agents={'id': SimpleNamespace(ws='new')},
                              registered_agents={'id': {'status': 'online'}}, decommissioned_agents=set())
        ns['unregister_connection'](obj, 'id', 'old')
        self.assertEqual(obj.registered_agents['id']['status'], 'online')
        self.assertIn('id', obj.active_agents)

    def test_curtain_restore_never_injects_mouse_releases(self):
        fn = method('hvnc/mirror.py', 'TouchpadLock', 'restore')
        fn.decorator_list = []
        registry, user32, threading = MagicMock(), MagicMock(), MagicMock()
        ns = {'winreg': registry, 'user32': user32, 'threading': threading,
              'wintypes': ctypes.wintypes, 'logger': logging.getLogger('test')}
        exec(compile(ast.Module(body=[fn], type_ignores=[]), '<curtain>', 'exec'), ns)
        for _ in range(3):
            ns['restore'](SimpleNamespace(_orig_values={}))
        user32.mouse_event.assert_not_called()
        user32.SendInput.assert_not_called()
        self.assertGreater(registry.SetValueEx.call_count, 0)
        self.assertEqual(threading.Thread.return_value.start.call_count, 3)

    def test_helper_in_installer_and_ota_payloads(self):
        for filename in ('master_hub/generator.py', 'master_hub/ota_manager.py'):
            self.assertIn('"device_identity.py"', (ROOT / filename).read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
