"""CSM profile tests use temporary files and never operate real services."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

try:
    import tomllib
except ImportError:
    tomllib = None

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
SPEC = importlib.util.spec_from_file_location('configure_csm', SCRIPTS / 'configure-csm.py')
csm = importlib.util.module_from_spec(SPEC)
with patch.object(sys, 'path', [str(SCRIPTS)] + sys.path):
    SPEC.loader.exec_module(csm)


class CsmTests(unittest.TestCase):
    @unittest.skipUnless(tomllib, 'Python 3.11+ is needed to validate the template TOML')
    def test_profile_matches_requested_settings_and_keeps_authorization_enabled(self):
        profile = tomllib.loads(csm.profile('192.168.200.12'))
        self.assertEqual(profile['csm']['max_grid_current'], 32.0)
        self.assertEqual(profile['can']['node_id'], 2)
        self.assertEqual(profile['can']['channel_name'], 'can0')
        self.assertEqual(profile['can']['bitrate'], 500000)
        self.assertEqual(profile['contactor']['manager'], 'contactorless')
        self.assertEqual(profile['graph']['file_path'], '/usr/local/etc/evis-csm/evis-csm-1bmpu.dot')
        self.assertEqual(len(profile['evses']), 1)
        evse = profile['evses'][0]
        self.assertEqual(evse['ip'], '192.168.200.12')
        self.assertEqual(evse['auth']['endpoint'], '192.168.200.12:15716')
        self.assertFalse(evse['auto_start'])
        self.assertTrue(evse['auth_enabled'])
        for direction, current in (('charge', 30.0), ('discharge', 32.0)):
            self.assertEqual(evse['max_dc_' + direction + '_power'], 9000.0)
            self.assertEqual(evse['max_dc_' + direction + '_voltage'], 490.0)
            self.assertEqual(evse['max_dc_' + direction + '_current'], current)
            self.assertEqual(evse['max_ac_' + direction + '_current'], 32.0)
        self.assertFalse(profile['ocpp']['enabled'])
        self.assertFalse(profile['evixios'][0]['enabled'])
        self.assertEqual(profile['http']['port'], 8444)
        self.assertEqual(profile['modbus']['port'], 5552)

    def test_invalid_addresses_or_modified_template_are_rejected(self):
        for ip in ('0.0.0.0', '127.0.0.1', '224.1.2.3', '255.255.255.255', '::1', 'hostname', '1.2.3.4\ncommand'):
            with self.subTest(ip=ip), self.assertRaises(ValueError):
                csm.profile(ip)
        with tempfile.TemporaryDirectory() as temporary:
            template = Path(temporary) / 'template'
            template.write_text('modified')
            with patch.object(csm, 'TEMPLATE', template), self.assertRaisesRegex(RuntimeError, 'reviewed profile'):
                csm.profile('192.168.200.11')

    def test_preview_and_non_root_apply_do_not_write_or_operate_services(self):
        for arguments in ([], ['--apply']):
            with self.subTest(arguments=arguments), patch.object(csm.sys, 'argv', ['configure-csm.py'] + arguments), \
                    patch.object(csm.os, 'geteuid', return_value=1000), \
                    patch.object(csm, 'apply_profile') as apply, contextlib.redirect_stdout(io.StringIO()) as output:
                if arguments:
                    with self.assertRaisesRegex(RuntimeError, 'sudo'):
                        csm.main()
                else:
                    csm.main()
                    self.assertIn('Preview only', output.getvalue())
                apply.assert_not_called()

    def test_apply_preserves_vendor_files_backs_up_and_does_not_control_services(self):
        for case in ('regular', 'symlink', 'outside-link', 'missing-graph', 'active', 'unknown-service',
                     'service-error', 'managed-symlink', 'managed-existing', 'same'):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                directory = root / 'csm'
                directory.mkdir()
                canonical = directory / 'evis-csm.toml'
                vendor = directory / 'vendor.toml'
                original = b'original vendor configuration and private values\n'
                content = csm.profile('192.168.200.11')
                vendor.write_bytes(original)
                if case == 'symlink':
                    canonical.symlink_to(vendor.name)
                elif case == 'outside-link':
                    external = root / 'outside.toml'
                    external.write_bytes(original)
                    canonical.symlink_to(external)
                else:
                    canonical.write_text(content if case == 'same' else original.decode())
                graph = directory / 'evis-csm-1bmpu.dot'
                if case != 'missing-graph':
                    graph.write_text('existing vendor graph')
                if case == 'managed-symlink':
                    (directory / 'evis-csm-wattwell.toml').symlink_to(vendor.name)
                if case == 'managed-existing':
                    (directory / 'evis-csm-wattwell.toml').write_text('previous managed profile')
                state = root / 'state'
                status = ('LoadState=' + ('not-found' if case == 'unknown-service' else 'loaded') + '\n'
                          'ActiveState=' + ('active' if case == 'active' else 'inactive') + '\n')
                result = subprocess.CompletedProcess([], int(case == 'service-error'), status, '')
                with patch.object(csm, 'CONFIG_DIR', directory), patch.object(csm, 'STATE', state), \
                        patch.object(csm.configure, 'run', return_value=result) as command:
                    if case in ('regular', 'symlink', 'managed-existing', 'same'):
                        csm.apply_profile(content)
                    else:
                        with self.assertRaises(RuntimeError):
                            csm.apply_profile(content)
                        self.assertEqual(canonical.read_bytes(), original)
                        self.assertFalse(state.exists())
                    if command.called:
                        self.assertEqual(command.call_args[0][0], ['systemctl', 'show', 'evis-csm.service',
                                                                 '--property=LoadState', '--property=ActiveState'])
                self.assertEqual(vendor.read_bytes(), original)
                if graph.exists():
                    self.assertEqual(graph.read_text(), 'existing vendor graph')
                if case in ('regular', 'symlink', 'managed-existing'):
                    self.assertTrue(canonical.is_symlink())
                    self.assertEqual(canonical.read_text(), content)
                    backup = next(state.glob('backup-*'))
                    self.assertEqual((backup / 'evis-csm.toml').read_bytes(), original)
                    metadata = json.loads((backup / 'original.json').read_text())
                    self.assertEqual(metadata['symlink'], vendor.name if case == 'symlink' else None)
                    if case == 'managed-existing':
                        self.assertEqual((backup / 'managed-before.toml').read_text(), 'previous managed profile')
                    self.assertEqual(state.stat().st_mode & 0o777, 0o700)
                    with patch.object(csm, 'CONFIG_DIR', directory), patch.object(csm, 'STATE', state), \
                            patch.object(csm.configure, 'run') as command:
                        csm.apply_profile(content)
                        command.assert_not_called()
                    self.assertEqual(len(list(state.glob('backup-*'))), 1)


if __name__ == '__main__':
    unittest.main()
