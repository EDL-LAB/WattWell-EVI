"""Commissioning tests use temporary identities and never operate real services."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import pwd
import shutil
import ssl
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock

ROOT = Path(__file__).resolve().parents[1]
with patch.object(sys, 'path', [str(ROOT / 'setup')] + sys.path):
    spec = importlib.util.spec_from_file_location('csm_https_setup', ROOT / 'setup/csm_https.py')
    setup = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(setup)


@unittest.skipUnless(shutil.which('openssl'), 'OpenSSL is required for certificate checks')
class SetupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name)
        cls.data = dict(evi_name='evi3.local', evi_ip='192.168.15.106', rpi_name='evcharger.local',
                        rpi_ip='192.168.15.132', ca_name='rpi-grid-controller.local', ca_ip='192.168.15.139',
                        ca_port=9000, https_port=8443, csm_port=8444, evse_count=1)
        cls.real_run = staticmethod(setup.pki.run)
        cls.real_run('openssl', 'req', '-x509', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:prime256v1',
                     '-nodes', '-keyout', cls.root / 'ca.key', '-out', cls.root / 'ca.pem', '-days', '2',
                     '-subj', '/CN=TemporaryCA', '-addext', 'basicConstraints=critical,CA:TRUE',
                     '-addext', 'keyUsage=critical,keyCertSign,cRLSign')
        cls.directories = {}
        for device, stem, cn, san, purpose in [('evi', 'server', 'evi3.local', 'evi3.local', 'serverAuth'),
                ('rpi', 'client', 'evcharger-csm-client', 'evcharger-csm-client.local', 'clientAuth')]:
            directory = cls.root / device
            directory.mkdir(mode=0o700)
            cls.directories[device] = directory
            cls.real_run('openssl', 'req', '-new', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:prime256v1',
                         '-nodes', '-keyout', directory / (stem + '.key'), '-out', directory / 'request.csr', '-subj', '/CN=' + cn)
            (directory / (stem + '.key')).chmod(0o600)
            extension = directory / 'extensions'
            extension.write_text('basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\n'
                                 'extendedKeyUsage=' + purpose + '\nsubjectAltName=DNS:' + san + '\n')
            cls.real_run('openssl', 'x509', '-req', '-in', directory / 'request.csr', '-CA', cls.root / 'ca.pem',
                         '-CAkey', cls.root / 'ca.key', '-CAcreateserial', '-days', '1', '-extfile', extension,
                         '-out', directory / (stem + '-chain.pem'))
            for name in ('root-ca.pem', 'client-ca.pem'):
                shutil.copyfile(cls.root / 'ca.pem', directory / name)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def unprivileged_path(self, path, directory=False):
        path = Path(path)
        if path.is_symlink() or not (path.is_dir() if directory else path.is_file()):
            raise ValueError('Unsafe temporary path')
        return path

    def test_proxy_render_and_real_identity_validation(self):
        data = dict(self.data, evi_name='other.local', https_port=18443, csm_port=18444)
        rendered = setup.proxy_config(data)
        self.assertIn('https://other.local:18443', rendered)
        self.assertIn('reverse_proxy 127.0.0.1:18444', rendered)
        self.assertIn('mode require_and_verify', rendered)
        self.assertIn('CN=evcharger-csm-client', rendered)
        self.assertIn('CapabilityBoundingSet=', setup.proxy_unit())
        self.assertNotIn('Wants=evis-csm', setup.proxy_unit())
        with patch.object(setup, 'protected', side_effect=self.unprivileged_path), \
                patch.dict(setup.pki.DEVICE_DIRS, self.directories), contextlib.redirect_stdout(io.StringIO()):
            for device in ('evi', 'rpi'):
                setup.material(device, self.data)
            with self.assertRaises(subprocess.CalledProcessError):
                setup.material('evi', dict(self.data, evi_name='wrong.local'))
            def expired(*args):
                if '-checkend' in args:
                    raise subprocess.CalledProcessError(1, args)
                return self.real_run(*args)
            with patch.object(setup.pki, 'run', side_effect=expired), self.assertRaisesRegex(ValueError, 'one hour'):
                setup.material('evi', self.data)

    def test_proxy_apply_and_retry_do_not_start_csm_or_change_vendor_settings(self):
        for answers in ([False], [True, False], [True, True]):
            with self.subTest(answers=answers), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                directory = root / 'tls'
                shutil.copytree(self.directories['evi'], directory)
                config = root / 'evis-csm.toml'
                original = '[http]\nenabled=true\nip="127.0.0.1"\nport=8444\n[modbus]\nenabled=false\n'
                config.write_text(original)
                commands = []
                def command(*args):
                    commands.append(tuple(str(arg) for arg in args))
                    if args[0] == 'systemctl':
                        return b'LoadState=loaded\nActiveState=inactive\n'
                    if str(args[0]) == str(setup.CADDY):
                        return b'v2.11.9 test' if args[1] == 'version' else b''
                    return self.real_run(*args)
                unit = root / 'proxy.service'
                with patch.object(setup, 'CSM_CONFIG', config), patch.object(setup, 'SERVICE', unit), \
                        patch.object(setup, 'protected', side_effect=self.unprivileged_path), \
                        patch.dict(setup.pki.DEVICE_DIRS, {'evi': directory}), \
                        patch.object(setup, 'CADDY', config), patch.object(setup.pki, 'run', side_effect=command), \
                        patch.object(setup.pki, 'yes', side_effect=answers), contextlib.redirect_stdout(io.StringIO()):
                    setup.evi(self.data, True)
                self.assertEqual(config.read_text(), original)
                if answers[0]:
                    self.assertEqual(unit.read_text(), setup.proxy_unit())
                    self.assertEqual((directory / 'Caddyfile').read_text(), setup.proxy_config(self.data))
                else:
                    self.assertFalse(unit.exists())
                    self.assertFalse((directory / 'Caddyfile').exists())
                mutations = [call for call in commands if call[0] == 'systemctl' and call[1] != 'show']
                self.assertTrue(all(call[1] == 'daemon-reload' or call[-1] == unit.name for call in mutations))
                self.assertEqual(any(call[1] == 'restart' for call in mutations), len(answers) == 2 and answers[1])

    def test_exposed_or_active_csm_is_refused(self):
        for ip, enabled, state in [('0.0.0.0', 'false', 'inactive'), ('127.0.0.1', 'true', 'inactive'),
                                   ('127.0.0.1', 'false', 'active')]:
            with self.subTest(ip=ip, enabled=enabled, state=state), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / 'evis-csm.toml'
                path.write_text('[http]\nenabled=true\nip="' + ip + '"\nport=8444\n[modbus]\nenabled=' + enabled + '\n')
                with patch.object(setup, 'CSM_CONFIG', path), patch.object(setup, 'protected', side_effect=self.unprivileged_path), \
                        patch.object(setup.pki, 'run', return_value=('LoadState=loaded\nActiveState=' + state + '\n').encode()), \
                        self.assertRaises(ValueError):
                    setup.check_csm(self.data)

    def test_staging_checks_the_live_loopback_api_without_stopping_csm(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / 'evis-csm.toml'
            original = '[http]\nenabled=true\nip="0.0.0.0"\nport=8444\n'
            config.write_text(original)
            with patch.object(setup, 'CSM_CONFIG', config), \
                    patch.object(setup, 'protected', side_effect=AssertionError('Staging must not read the CSM TOML')), \
                    patch.object(setup.http.client, 'HTTPConnection') as connection, \
                    patch.object(setup.pki, 'run', return_value=b'active\n') as service_command:
                connection.return_value.getresponse.return_value.status = 200
                setup.check_csm(self.data, stage=True)
                connection.assert_called_once_with('127.0.0.1', self.data['csm_port'], timeout=3)
                connection.return_value.request.assert_called_once_with('GET', '/api/v1/csm/status')
                service_command.assert_called_once_with('systemctl', 'show', 'evis-csm.service', '-p', 'ActiveState', '--value')
                connection.return_value.getresponse.return_value.status = 503
                with self.assertRaisesRegex(ValueError, 'HTTP 200'):
                    setup.check_csm(self.data, stage=True)
                service_command.return_value = b'inactive\n'
                with self.assertRaisesRegex(ValueError, 'service to be running'):
                    setup.check_csm(self.data, stage=True)
            self.assertEqual(config.read_text(), original)

    def test_rpi_install_reuses_identity_restricts_runtime_access_and_is_repeatable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo, runtime = root / 'repo', root / 'runtime'
            compiled = repo / 'backend/dist/services/csm'
            compiled.mkdir(parents=True)
            (compiled / 'CSMTLS.js').write_text('connection.json')
            (compiled / 'RealCSMService.js').write_text('rejectUnauthorized: true')
            account = pwd.getpwuid(os.getuid())
            key_before = (self.directories['rpi'] / 'client.key').read_bytes()
            with patch.object(setup, 'REPO', repo), patch.object(setup, 'RUNTIME', runtime), \
                    patch.object(setup, 'protected', side_effect=self.unprivileged_path), \
                    patch.dict(setup.pki.DEVICE_DIRS, self.directories), patch.object(setup, 'backend_account', return_value=account), \
                    patch.object(setup.os, 'chown'), patch.object(setup.pki, 'yes', return_value=True), \
                    contextlib.redirect_stdout(io.StringIO()):
                def command(*args):
                    return b'' if args[0] == 'runuser' else self.real_run(*args)
                with patch.object(setup.pki, 'run', side_effect=command) as run:
                    setup.rpi(self.data, False)
                    self.assertFalse(runtime.exists())
                    setup.rpi(self.data, True)
                    setup.rpi(self.data, True)
                    self.assertFalse(any(args[0][0] == 'systemctl' for args, _ in run.call_args_list))
            self.assertEqual((runtime / 'client.key').read_bytes(), key_before)
            self.assertEqual((self.directories['rpi'] / 'client.key').stat().st_mode & 0o777, 0o600)
            self.assertEqual((runtime / 'client.key').stat().st_mode & 0o777, 0o640)
            self.assertEqual(runtime.stat().st_mode & 0o777, 0o750)
            self.assertEqual(json.loads((runtime / 'connection.json').read_text()),
                             dict(host='evi3.local', port=8443, address='192.168.15.106'))
            self.assertFalse(list(runtime.glob('*.backup-*')))

    def test_rpi_checks_reject_anonymous_access_or_a_direct_http_bypass(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary)
            for target, source in [('root-ca.pem', 'root-ca.pem'), ('client-chain.pem', 'client-chain.pem'), ('client.key', 'client.key')]:
                shutil.copyfile(self.directories['rpi'] / source, runtime / target)
            (runtime / 'connection.json').write_text(json.dumps(dict(host='evi3.local', port=8443, address='192.168.15.106')))
            for anonymous, direct in [(ssl.SSLError('certificate required'), ConnectionRefusedError()),
                                      (200, ConnectionRefusedError()), (ssl.SSLError('certificate required'), Mock())]:
                with self.subTest(anonymous=anonymous, direct=direct), patch.object(setup, 'RUNTIME', runtime), \
                        patch.object(setup, 'protected', side_effect=self.unprivileged_path), \
                        patch.object(setup, 'read_status', side_effect=[200, anonymous]), \
                        patch.object(setup.socket, 'create_connection', side_effect=direct if isinstance(direct, Exception) else None, return_value=direct), \
                        contextlib.redirect_stdout(io.StringIO()):
                    if isinstance(anonymous, ssl.SSLError) and isinstance(direct, ConnectionRefusedError):
                        setup.check_rpi(self.data)
                    else:
                        with self.assertRaises(ValueError):
                            setup.check_rpi(self.data)

    def test_missing_runtime_explains_apply_before_check_without_network_access(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(setup, 'RUNTIME', Path(temporary) / 'not-installed'), \
                    patch.object(setup.socket, 'create_connection') as connect, \
                    self.assertRaisesRegex(ValueError, '--apply from the backend checkout'):
                setup.check_rpi(self.data)
            connect.assert_not_called()

    def test_backend_account_requires_the_installed_checkout_and_nonroot_user(self):
        account = pwd.getpwuid(os.getuid())
        for status, code, allowed in [
            ('LoadState=loaded\nUser=' + account.pw_name + '\nWorkingDirectory=' + str(setup.REPO) + '\n', 0, True),
            ('LoadState=not-found\n', 1, True),
            ('LoadState=loaded\nUser=' + account.pw_name + '\nWorkingDirectory=/another/checkout\n', 0, False),
            ('', 1, False),
            ('LoadState=loaded\nUser=' + account.pw_name + '\nWorkingDirectory=\n', 0, False),
            ('LoadState=loaded\nUser=root\nWorkingDirectory=' + str(setup.REPO) + '\n', 0, False),
        ]:
            with self.subTest(status=status), patch.object(setup.subprocess, 'run', return_value=                    subprocess.CompletedProcess([], code, stdout=status, stderr='')), \
                    patch.object(setup.pki, 'ask', return_value=account.pw_name):
                if allowed:
                    self.assertEqual(setup.backend_account().pw_uid, account.pw_uid)
                else:
                    with self.assertRaises(ValueError):
                        setup.backend_account()

    def test_protected_paths_refuse_symlinks_and_unmanaged_destinations(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / 'target'
            target.write_text('unmanaged')
            link = root / 'link'
            link.symlink_to(target)
            with self.assertRaises(ValueError):
                setup.protected(link)
            with patch.object(setup, 'protected', side_effect=self.unprivileged_path), self.assertRaisesRegex(ValueError, 'unmanaged'):
                setup.managed_destination(target)


if __name__ == '__main__':
    unittest.main()
