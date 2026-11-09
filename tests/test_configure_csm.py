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
    def run_main(self):
        with patch.object(csm, 'detect_version', return_value='2.5.0'):
            csm.main()

    def test_detects_only_an_exact_supported_installed_version(self):
        for version in ('2.5.0', '2.9.0', '2.11.0'):
            banner = 'Program : evis-csm - Version : v' + version + '\nCommit : test\nAuthor : Watt&Well\n'
            with self.subTest(version=version), patch.object(csm.subprocess, 'run', return_value=                    subprocess.CompletedProcess([], 0, banner, '')) as command:
                self.assertEqual(csm.detect_version(), version)
                self.assertEqual(command.call_args[0][0], [csm.CSM_EXECUTABLE, '--version'])
                self.assertEqual(command.call_args[1]['timeout'], 10)
        for banner, code in [('', 0), ('Program : other - Version : v2.9.0\n', 0),
                             ('Program : evis-csm - Version : v2.9.0-dev\n', 0),
                             ('Program : evis-csm - Version : v2.9.0\n', 1),
                             ('Program : evis-csm - Version : v3.0.0\n', 0),
                             ('Program : evis-csm - Version : v2.5.0\nProgram : evis-csm - Version : v2.9.0\n', 0)]:
            with self.subTest(banner=banner, code=code), patch.object(csm.subprocess, 'run', return_value=                    subprocess.CompletedProcess([], code, banner, '')), self.assertRaises(RuntimeError):
                csm.detect_version()
        for error in (OSError('missing'), subprocess.TimeoutExpired('evis-csm', 10)):
            with patch.object(csm.subprocess, 'run', side_effect=error), self.assertRaises(RuntimeError):
                csm.detect_version()

    def test_main_uses_actual_version_profile_and_never_creates_enable_flag(self):
        with tempfile.TemporaryDirectory() as temporary:
            enable_flag = Path(temporary) / 'evis-csm_is_enabled'
            for version in ('2.5.0', '2.9.0', '2.11.0'):
                with self.subTest(version=version), patch.object(csm, 'ENABLE_FLAG', enable_flag), \
                        patch.object(csm, 'detect_version', return_value=version) as detected, \
                        patch.object(csm.sys, 'argv', ['configure-csm.py', '--apply', '--topology', '1bmpu']), \
                        patch.object(csm.sys.stdin, 'isatty', return_value=False), \
                        patch.object(csm.os, 'geteuid', return_value=0), patch.object(csm.os, 'umask'), \
                        patch.object(csm, 'apply_profile') as apply, contextlib.redirect_stdout(io.StringIO()) as output:
                    csm.main()
                    detected.assert_called_once_with()
                    self.assertIn('Detected installed CSM: v' + version, output.getvalue())
                    self.assertEqual('auto_start = false' in output.getvalue(), version == '2.11.0')
                    self.assertEqual('EVIX-IO explicitly disabled' in output.getvalue(), version == '2.11.0')
                    self.assertEqual('activation flag is absent' in output.getvalue(), version in ('2.9.0', '2.11.0'))
                    apply.assert_called_once_with(csm.profile('192.168.200.11', version=version), '1bmpu')
                    self.assertFalse(enable_flag.exists())
        with patch.object(csm, 'detect_version', side_effect=RuntimeError('unreviewed version')), \
                patch.object(csm.sys, 'argv', ['configure-csm.py', '--apply', '--topology', '1bmpu']), \
                patch.object(csm.os, 'geteuid', return_value=0), patch.object(csm, 'apply_profile') as apply:
            with self.assertRaisesRegex(RuntimeError, 'unreviewed version'):
                csm.main()
            apply.assert_not_called()

    @unittest.skipUnless(tomllib, 'Python 3.11+ is needed to validate the template TOML')
    def test_profile_matches_requested_settings_and_keeps_authorization_enabled(self):
        profile = tomllib.loads(csm.profile('192.168.200.12'))
        self.assertEqual(profile['csm']['max_grid_current'], 32.0)
        self.assertEqual(profile['can']['node_id'], 2)
        self.assertEqual(profile['can']['channel_name'], 'can0')
        self.assertEqual(profile['can']['bitrate'], 500000)
        self.assertEqual(profile['contactor']['manager'], 'contactorless')
        self.assertEqual(profile['graph']['file_path'], '/usr/local/etc/evis-csm/evis-csm-wattwell-1bmpu.dot')
        self.assertEqual(len(profile['evses']), 1)
        evse = profile['evses'][0]
        self.assertEqual(evse['ip'], '192.168.200.12')
        self.assertEqual(evse['auth']['endpoint'], '192.168.200.12:15716')
        self.assertNotIn('auto_start', evse)
        self.assertTrue(evse['auth_enabled'])
        for direction, current in (('charge', 30.0), ('discharge', 32.0)):
            self.assertEqual(evse['max_dc_' + direction + '_power'], 9000.0)
            self.assertEqual(evse['max_dc_' + direction + '_voltage'], 490.0)
            self.assertEqual(evse['max_dc_' + direction + '_current'], current)
            self.assertEqual(evse['max_ac_' + direction + '_current'], 32.0)
        self.assertFalse(profile['ocpp']['enabled'])
        self.assertNotIn('evixios', profile)
        self.assertEqual(profile['http']['port'], 8444)
        self.assertEqual(profile['http']['ip'], '127.0.0.1')
        self.assertFalse(profile['modbus']['enabled'])
        self.assertEqual(profile['modbus']['ip'], '127.0.0.1')
        self.assertEqual(profile['modbus']['port'], 5552)

    @unittest.skipUnless(tomllib, 'Python 3.11+ is needed to validate TOML')
    def test_network_settings_apply_to_all_topologies_and_preserve_hardware_settings(self):
        for topology in csm.TOPOLOGIES:
            for address in ('disabled', '127.0.0.1', '192.168.200.11'):
                with self.subTest(topology=topology, address=address):
                    default = tomllib.loads(csm.profile('192.168.200.11', topology))
                    configured = tomllib.loads(csm.profile('192.168.200.11', topology, 18444, address, 15552))
                    self.assertEqual(configured['http']['ip'], '127.0.0.1')
                    self.assertEqual(configured['http']['port'], 18444)
                    self.assertEqual(configured['modbus']['enabled'], address != 'disabled')
                    self.assertEqual(configured['modbus']['ip'], '127.0.0.1' if address == 'disabled' else address)
                    self.assertEqual(configured['modbus']['port'], 15552)
                    default.pop('http')
                    default.pop('modbus')
                    configured.pop('http')
                    configured.pop('modbus')
                    self.assertEqual(configured, default)

    def test_invalid_network_settings_are_rejected(self):
        with self.assertRaisesRegex(ValueError, 'Unreviewed CSM version'):
            csm.profile('192.168.200.11', version='2.12.0')
        for port in (0, 65536, -1, True, '8444\ncommand', '1.5', '１２'):
            for option in ('http_port', 'modbus_port'):
                with self.subTest(port=port, option=option), self.assertRaises(ValueError):
                    csm.profile('192.168.200.11', **{option: port})
        for address in ('0.0.0.0', '224.1.2.3', '255.255.255.255', '::1', 'localhost', "127.0.0.1'\ncommand"):
            with self.subTest(address=address), self.assertRaises(ValueError):
                csm.profile('192.168.200.11', modbus_ip=address)
        with self.assertRaisesRegex(ValueError, 'different ports'):
            csm.profile('192.168.200.11', http_port=5552, modbus_ip='127.0.0.1')

    def test_interactive_network_defaults_and_explicit_cli_values(self):
        cases = [
            (['--apply'], ['1', '', '', ''], '192.168.200.11', '1bmpu', 8444, 'disabled', 5552),
            (['--apply'], ['2', '192.168.200.12', '0', '18444', '0.0.0.0', '127.0.0.1', '15552'],
             '192.168.200.12', '2bmpu-parallel', 18444, '127.0.0.1', 15552),
            (['--apply', '--topology', '2bmpu-series', '--evse-ip', '192.168.200.12',
              '--http-port', '18444', '--modbus-ip', '192.168.200.11', '--modbus-port', '15552'], [],
             '192.168.200.12', '2bmpu-series', 18444, '192.168.200.11', 15552),
        ]
        for arguments, answers, address, topology, http_port, modbus_ip, modbus_port in cases:
            with self.subTest(arguments=arguments, answers=answers), \
                    patch.object(csm.sys, 'argv', ['configure-csm.py'] + arguments), \
                    patch.object(csm.sys.stdin, 'isatty', return_value=True), \
                    patch('builtins.input', side_effect=answers) as prompt, \
                    patch.object(csm.os, 'geteuid', return_value=0), patch.object(csm.os, 'umask'), \
                    patch.object(csm, 'apply_profile') as apply, contextlib.redirect_stdout(io.StringIO()):
                self.run_main()
                self.assertEqual(prompt.call_count, len(answers))
                apply.assert_called_once_with(csm.profile(address, topology, http_port, modbus_ip, modbus_port), topology)

    def test_noninteractive_defaults_and_invalid_settings_do_not_apply(self):
        with patch.object(csm.sys.stdin, 'isatty', return_value=False), \
                patch('builtins.input', side_effect=AssertionError('Unexpected prompt')), \
                patch.object(csm.sys, 'argv', ['configure-csm.py', '--topology', '1bmpu']), \
                patch.object(csm, 'apply_profile') as apply, contextlib.redirect_stdout(io.StringIO()) as output:
            self.run_main()
            self.assertIn('CSM HTTP: 127.0.0.1:8444', output.getvalue())
            self.assertIn('Modbus: disabled', output.getvalue())
            apply.assert_not_called()
        for options in (['--http-port', '0'], ['--modbus-ip', '0.0.0.0'],
                        ['--http-port', '5552', '--modbus-ip', '127.0.0.1']):
            with self.subTest(options=options), patch.object(csm.sys.stdin, 'isatty', return_value=False), \
                    patch.object(csm.sys, 'argv', ['configure-csm.py', '--apply', '--topology', '1bmpu'] + options), \
                    patch.object(csm.os, 'geteuid', return_value=0), patch.object(csm, 'apply_profile') as apply, \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises((SystemExit, ValueError)):
                    self.run_main()
                apply.assert_not_called()

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
            with self.subTest(arguments=arguments), patch.object(csm.sys, 'argv', ['configure-csm.py', '--topology', '1bmpu'] + arguments), \
                    patch.object(csm.sys.stdin, 'isatty', return_value=False), \
                    patch.object(csm.os, 'geteuid', return_value=1000), \
                    patch.object(csm, 'apply_profile') as apply, contextlib.redirect_stdout(io.StringIO()) as output:
                if arguments:
                    with self.assertRaisesRegex(RuntimeError, 'sudo'):
                        self.run_main()
                else:
                    self.run_main()
                    self.assertIn('Preview only', output.getvalue())
                apply.assert_not_called()

    def test_apply_preserves_vendor_files_backs_up_and_does_not_control_services(self):
        for case in ('regular', 'symlink', 'outside-link', 'graph-symlink', 'active', 'unknown-service',
                     'service-error', 'managed-symlink', 'managed-existing', 'same', 'custom-command'):
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
                graph.write_text('existing vendor graph')
                managed_graph = directory / 'evis-csm-wattwell-1bmpu.dot'
                if case == 'graph-symlink':
                    managed_graph.symlink_to(graph.name)
                else:
                    managed_graph.write_bytes(csm.graph_content('1bmpu') if case == 'same' else b'previous managed graph')
                if case == 'managed-symlink':
                    (directory / 'evis-csm-wattwell.toml').symlink_to(vendor.name)
                if case == 'managed-existing':
                    (directory / 'evis-csm-wattwell.toml').write_text('previous managed profile')
                state = root / 'state'
                override = root / '90-wattwell.conf'
                status = ('LoadState=' + ('not-found' if case == 'unknown-service' else 'loaded') + '\n'
                          'ActiveState=' + ('active' if case == 'active' else 'inactive') + '\n'
                          'ExecStart={ argv[]=' + csm.SERVICE_COMMAND +
                          (' --custom' if case == 'custom-command' else '') +
                          ' -g /usr/local/etc/evis-csm/starterkit.dot ; }\n')
                result = subprocess.CompletedProcess([], int(case == 'service-error'), status, '')
                with patch.object(csm, 'CONFIG_DIR', directory), patch.object(csm, 'STATE', state), patch.object(csm, 'SERVICE_DROP_IN', override), \
                        patch.object(csm.configure, 'run', return_value=result) as command:
                    if case in ('regular', 'symlink', 'managed-existing', 'same'):
                        csm.apply_profile(content)
                    else:
                        with self.assertRaises(RuntimeError):
                            csm.apply_profile(content)
                        self.assertEqual(canonical.read_bytes(), original)
                        self.assertFalse(state.exists())
                    if command.called:
                        self.assertEqual(command.call_args_list[0][0][0], ['systemctl', 'show', 'evis-csm.service',
                            '--property=LoadState', '--property=ActiveState', '--property=ExecStart'])
                        self.assertTrue(all(call[0][0] in (command.call_args_list[0][0][0],
                            ['systemctl', 'daemon-reload']) for call in command.call_args_list))
                self.assertEqual(vendor.read_bytes(), original)
                if graph.exists():
                    self.assertEqual(graph.read_text(), 'existing vendor graph')
                if case in ('regular', 'symlink', 'managed-existing', 'same'):
                    self.assertEqual(managed_graph.read_bytes(), csm.graph_content('1bmpu'))
                    self.assertTrue(canonical.is_symlink())
                    self.assertEqual(canonical.read_text(), content)
                    backup = next(state.glob('backup-*'))
                    self.assertEqual((backup / 'evis-csm.toml').read_bytes(), content.encode() if case == 'same' else original)
                    self.assertEqual((backup / 'graph-before.dot').read_bytes(),
                        csm.graph_content('1bmpu') if case == 'same' else b'previous managed graph')
                    metadata = json.loads((backup / 'original.json').read_text())
                    self.assertEqual(metadata['symlink'], vendor.name if case == 'symlink' else None)
                    if case == 'managed-existing':
                        self.assertEqual((backup / 'managed-before.toml').read_text(), 'previous managed profile')
                    self.assertEqual(state.stat().st_mode & 0o777, 0o700)
                    with patch.object(csm, 'CONFIG_DIR', directory), patch.object(csm, 'STATE', state), patch.object(csm, 'SERVICE_DROP_IN', override), \
                            patch.object(csm.configure, 'run', return_value=subprocess.CompletedProcess([], 0,
                                'LoadState=loaded\nActiveState=inactive\nExecStart={ argv[]=' + csm.SERVICE_COMMAND +
                                ' -g /usr/local/etc/evis-csm/evis-csm-wattwell-1bmpu.dot ; }\n', '')) as command:
                        csm.apply_profile(content)
                        self.assertEqual(command.call_count, 1)
                    self.assertEqual(len(list(state.glob('backup-*'))), 1)


    def test_override_backup_and_reload_failure_can_be_retried_without_starting_csm(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'evis-csm.toml').write_text('original')
            override = root / '90-wattwell.conf'
            override.write_text('previous override')
            content = csm.profile('192.168.200.11')
            status = subprocess.CompletedProcess([], 0,
                'LoadState=loaded\nActiveState=inactive\nExecStart={ argv[]=' + csm.SERVICE_COMMAND +
                ' -g /usr/local/etc/evis-csm/starterkit.dot ; }\n', '')
            with patch.object(csm, 'CONFIG_DIR', root), patch.object(csm, 'STATE', root / 'state'), \
                    patch.object(csm, 'SERVICE_DROP_IN', override):
                with patch.object(csm.configure, 'run', side_effect=[status, RuntimeError('reload failed')]):
                    with self.assertRaisesRegex(RuntimeError, 'reload failed'):
                        csm.apply_profile(content)
                backup = next((root / 'state').glob('backup-*'))
                self.assertEqual((backup / 'service-override-before.conf').read_text(), 'previous override')
                with patch.object(csm.configure, 'run', return_value=status) as command:
                    csm.apply_profile(content)
                    self.assertEqual(command.call_count, 2)
                    self.assertEqual(command.call_args[0][0], ['systemctl', 'daemon-reload'])

    def test_apply_uses_interactive_selection(self):
        for topology in csm.TOPOLOGIES:
            with self.subTest(topology=topology), \
                    patch.object(csm.sys, 'argv', ['configure-csm.py', '--apply']), \
                    patch.object(csm.sys.stdin, 'isatty', return_value=False), \
                    patch.object(csm.os, 'geteuid', return_value=0), patch.object(csm.os, 'umask'), \
                    patch.object(csm, 'choose_topology', return_value=topology) as choose, \
                    patch.object(csm, 'apply_profile') as apply, contextlib.redirect_stdout(io.StringIO()):
                self.run_main()
                choose.assert_called_once_with()
                apply.assert_called_once_with(csm.profile('192.168.200.11', topology), topology)

    def test_menu_requires_explicit_valid_choice_and_noninteractive_option(self):
        for number, topology in enumerate(csm.TOPOLOGIES, 1):
            with self.subTest(topology=topology), patch.object(csm.sys.stdin, 'isatty', return_value=True), \
                    patch('builtins.input', side_effect=['invalid', str(number)]), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(csm.choose_topology(), topology)
        with patch.object(csm.sys.stdin, 'isatty', return_value=False), self.assertRaisesRegex(RuntimeError, '--topology'):
            csm.choose_topology()

    @unittest.skipUnless(tomllib, 'Python 3.11+ is needed to validate TOML')
    def test_each_topology_installs_its_graph_without_touching_vendor_graphs(self):
        for version, topology in ((v, t) for v in ('2.5.0', '2.9.0', '2.11.0') for t in csm.TOPOLOGIES):
            with self.subTest(version=version, topology=topology), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                canonical = directory / 'evis-csm.toml'
                canonical.write_text('original')
                content = csm.profile('192.168.200.11', topology, 18444, '127.0.0.1', 15552, version=version)
                parsed = tomllib.loads(content)
                graph_name = 'evis-csm-wattwell-' + topology + '.dot'
                self.assertEqual(parsed['graph']['file_path'], '/usr/local/etc/evis-csm/' + graph_name)
                legacy = tomllib.loads(csm.profile('192.168.200.11', topology, 18444, '127.0.0.1', 15552))
                comparable = tomllib.loads(content)
                if version == '2.11.0':
                    self.assertIs(comparable['evses'][0].pop('auto_start'), False)
                    self.assertEqual(comparable.pop('evixios'), [dict(id=1, ip='127.0.0.1', sub_port=15516,
                                                                    req_port=15616, enabled=False, nb_boards=1)])
                self.assertEqual(comparable, legacy)
                self.assertEqual(parsed['evses'][0]['max_dc_charge_voltage'], 490.0)
                self.assertEqual(parsed['evses'][0]['max_dc_charge_power'], 9000.0)
                if version == '2.11.0':
                    self.assertIs(parsed['evses'][0]['auto_start'], False)
                else:
                    self.assertNotIn('auto_start', parsed['evses'][0])
                if version != '2.11.0':
                    self.assertNotIn('evixios', parsed)
                self.assertTrue(parsed['evses'][0]['auth_enabled'])
                self.assertEqual(parsed['http'], dict(enabled=True, ip='127.0.0.1', port=18444,
                                                     release_mode=True, pprof=False, ws_enabled=False))
                self.assertEqual(parsed['modbus']['ip'], '127.0.0.1')
                self.assertEqual(parsed['modbus']['port'], 15552)
                self.assertTrue(parsed['modbus']['enabled'])
                status = subprocess.CompletedProcess([], 0, 'LoadState=loaded\nActiveState=inactive\nExecStart={ argv[]=' + csm.SERVICE_COMMAND +
                    ' -g /usr/local/etc/evis-csm/starterkit.dot ; }\n', '')
                with patch.object(csm, 'CONFIG_DIR', directory), patch.object(csm, 'STATE', directory / 'state'), \
                        patch.object(csm, 'SERVICE_DROP_IN', directory / '90-wattwell.conf'), \
                        patch.object(csm.configure, 'run', return_value=status):
                    csm.apply_profile(content, topology)
                self.assertEqual(canonical.read_text(), content)
                self.assertIn('ExecStart=\nExecStart=' + csm.SERVICE_COMMAND +
                              ' -g /usr/local/etc/evis-csm/' + graph_name,
                              (directory / '90-wattwell.conf').read_text())
                self.assertEqual((directory / graph_name).read_bytes(), csm.graph_content(topology))
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(csm.configure, 'PROJECT', Path(temporary)):
                (Path(temporary) / 'config').mkdir()
                (Path(temporary) / 'config/csm-1bmpu.dot').write_text('changed')
                with self.assertRaisesRegex(RuntimeError, 'reviewed example'):
                    csm.graph_content('1bmpu')


if __name__ == '__main__':
    unittest.main()
