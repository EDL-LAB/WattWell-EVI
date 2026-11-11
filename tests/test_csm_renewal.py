"""Renewal tests use temporary keys and mock systemd; never use production identities."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

import test_csm_https_setup as fixtures

ROOT = Path(__file__).resolve().parents[1]
with patch.object(sys, 'path', [str(ROOT / 'setup')] + sys.path):
    spec = importlib.util.spec_from_file_location('csm_renewal', ROOT / 'setup/csm_renewal.py')
    renewal = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(renewal)


@unittest.skipUnless(shutil.which('openssl'), 'OpenSSL is needed for real identity validation')
class RenewalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.SetupTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def protected(self, path, directory=False):
        path = Path(path)
        if path.is_symlink() or not (path.is_dir() if directory else path.is_file()):
            raise ValueError('Unsafe test path')
        return path

    @contextlib.contextmanager
    def local(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dirs = {}
            for device, source in self.directories.items():
                dirs[device] = root / device
                shutil.copytree(source, dirs[device])
                (root / 'state' / device).mkdir(parents=True)
            installed = root / 'installed'
            installed.mkdir()
            (installed / 'step').write_text('not executed in unit tests')
            with patch.object(renewal.https, 'protected', side_effect=self.protected), \
                    patch.dict(renewal.pki.DEVICE_DIRS, dirs), patch.object(renewal.pki, 'STATE', root / 'state'), \
                    patch.object(renewal, 'INSTALL', installed), contextlib.redirect_stdout(io.StringIO()):
                yield root, dirs

    def test_not_due_and_expired_certificates_never_request_renewal(self):
        with self.local() as (_, dirs), patch.object(renewal, 'request_renewal') as request, \
                patch.object(renewal, 'refresh_evi') as evi, patch.object(renewal, 'refresh_rpi') as rpi:
            for device in ('evi', 'rpi'):
                key = dirs[device] / ('server.key' if device == 'evi' else 'client.key')
                original = key.read_bytes()
                renewal.renew(device, self.data)
                self.assertEqual(key.read_bytes(), original)
            request.assert_not_called()
            self.assertEqual(evi.call_count, 1)
            self.assertEqual(rpi.call_count, 1)
            with patch.object(renewal, 'expiry', return_value=time.time() - 1), self.assertRaisesRegex(ValueError, 'expired'):
                renewal.renew('evi', self.data)
            request.assert_not_called()

    def test_due_certificate_validated_before_install_and_private_key_preserved(self):
        for device, role in (('evi', 'server'), ('rpi', 'client')):
            for wrong_identity in (False, True):
                with self.subTest(device=device, wrong=wrong_identity), self.local() as (root, dirs):
                    directory = dirs[device]
                    chain, key = directory / (role + '-chain.pem'), directory / (role + '.key')
                    before, private = chain.read_bytes(), key.read_bytes()
                    candidate = root / 'renewed.pem'
                    self.real_run('openssl', 'x509', '-req', '-in', directory / 'request.csr',
                                  '-CA', directory / 'root-ca.pem', '-CAkey', self.root / 'ca.key',
                                  '-set_serial', '1005', '-days', '1', '-extfile', directory / 'extensions', '-out', candidate)
                    returned = (dirs['rpi' if device == 'evi' else 'evi'] /
                                ('client-chain.pem' if device == 'evi' else 'server-chain.pem')).read_bytes() if wrong_identity else candidate.read_bytes()
                    actual_expiry = renewal.expiry
                    def end(path):
                        return actual_expiry(path) - (17 * 3600 if Path(path) == chain else 0)
                    with patch.object(renewal, 'expiry', side_effect=end), \
                            patch.object(renewal, 'request_renewal', return_value=returned), \
                            patch.object(renewal, 'refresh_evi') as evi, patch.object(renewal, 'refresh_rpi') as rpi:
                        if wrong_identity:
                            with self.assertRaises(subprocess.CalledProcessError):
                                renewal.renew(device, self.data)
                            self.assertEqual(chain.read_bytes(), before)
                            evi.assert_not_called()
                            rpi.assert_not_called()
                        else:
                            renewal.renew(device, self.data)
                            self.assertEqual(chain.read_bytes(), candidate.read_bytes())
                            self.assertTrue(evi.called if device == 'evi' else rpi.called)
                        self.assertEqual(key.read_bytes(), private)

    def test_refresh_rpi_changes_only_public_chain_and_preserves_permissions(self):
        with self.local() as (root, dirs):
            runtime = root / 'runtime'
            runtime.mkdir()
            config = {'host': self.data['evi_name'], 'port': self.data['https_port'], 'address': self.data['evi_ip']}
            (runtime / 'connection.json').write_text(json.dumps(config))
            for name in ('client.key', 'root-ca.pem'):
                shutil.copyfile(dirs['rpi'] / name, runtime / name)
            target = runtime / 'client-chain.pem'
            target.write_text('old public chain')
            target.chmod(0o640)
            before = {p.name: p.read_bytes() for p in runtime.iterdir() if p != target}
            with patch.object(renewal.https, 'RUNTIME', runtime), patch.object(renewal.os, 'chown') as chown:
                renewal.renew('rpi', self.data, refresh_only=True)
                self.assertEqual(target.read_bytes(), (dirs['rpi'] / 'client-chain.pem').read_bytes())
                self.assertEqual(target.stat().st_mode & 0o777, 0o640)
                self.assertEqual({p.name: p.read_bytes() for p in runtime.iterdir() if p.name in before}, before)
                renewal.renew('rpi', self.data, refresh_only=True)
                chown.assert_called_once()
                (runtime / 'root-ca.pem').write_text('unexpected root')
                with self.assertRaisesRegex(ValueError, 'key/trust'):
                    renewal.renew('rpi', self.data, refresh_only=True)

    def test_refresh_evi_signals_only_proxy_and_checks_presented_certificate(self):
        with self.local() as (root, dirs):
            directory = dirs['evi']
            (directory / 'Caddyfile').write_text(renewal.https.proxy_config(self.data))
            unit = root / 'proxy.service'
            unit.write_text(renewal.https.proxy_unit())
            commands = []
            def run(*args):
                commands.append(tuple(map(str, args)))
                if args[0] == 'systemctl': return b''
                if str(args[0]) == str(renewal.https.CADDY):
                    return b'v2.11.9 test' if args[1] == 'version' else b''
                return self.real_run(*args)
            context = MagicMock()
            context.wrap_socket.return_value.__enter__.return_value.getpeercert.return_value = \
                self.real_run('openssl', 'x509', '-in', directory / 'server-chain.pem', '-outform', 'DER')
            with patch.object(renewal.https, 'SERVICE', unit), patch.object(renewal.pki, 'run', side_effect=run), \
                    patch.object(renewal.ssl, 'create_default_context', return_value=context), \
                    patch.object(renewal.socket, 'create_connection', return_value=MagicMock()):
                renewal.renew('evi', self.data, refresh_only=True)
                renewal.renew('evi', self.data, refresh_only=True)
                signals = [c for c in commands if c[0:2] == ('systemctl', 'kill')]
                self.assertEqual(signals, [('systemctl', 'kill', '--kill-who=main', '--signal=SIGUSR1', unit.name)])
                self.assertFalse(any('restart' in c or 'stop' in c or 'evis-csm.service' in c for c in commands))
                (directory / 'Caddyfile').write_text('unreviewed config')
                with self.assertRaisesRegex(ValueError, 'differs'):
                    renewal.renew('evi', self.data, refresh_only=True)

    def test_install_copies_root_timer_code_and_never_restarts_charging_services(self):
        with self.local() as (root, dirs):
            install, units = root / 'new-install', root / 'units'
            units.mkdir()
            commands = []
            def run(*args):
                commands.append(tuple(map(str, args)))
                return b'' if args[0] == 'systemctl' else self.real_run(*args)
            with patch.object(renewal, 'INSTALL', install), patch.object(renewal, 'UNITS', units), \
                    patch.object(renewal, 'install_step'), patch.object(renewal, 'verified_renewal'), \
                    patch.object(renewal, 'renew'), patch.object(renewal.pki, 'yes', return_value=True), \
                    patch.object(renewal.pki, 'run', side_effect=run):
                renewal.install('evi', self.data)
                renewal.install('evi', self.data)
            service = (units / (renewal.NAME + '.service')).read_text()
            self.assertIn(str(install / 'setup/csm_renewal.py') + ' --run evi', service)
            self.assertNotIn(str(ROOT), service)
            self.assertIn('ProtectSystem=strict', service)
            self.assertEqual((install / 'config/csm-https.Caddyfile.example').read_bytes(),
                             (ROOT / 'config/csm-https.Caddyfile.example').read_bytes())
            mutations = [c for c in commands if c[0] == 'systemctl']
            self.assertTrue(all(c[1] == 'daemon-reload' or c == ('systemctl', 'enable', '--now', renewal.NAME + '.timer') for c in mutations))

    def test_request_keeps_client_renewal_separate_from_scoped_server_issuance(self):
        for device, role in (('evi', 'server'), ('rpi', 'client')):
            with self.subTest(role=role), self.local() as (_, dirs):
                directory = dirs[device]
                if role == 'server':
                    shutil.copyfile(directory / 'request.csr', directory / 'server.csr')
                chain, key, root = directory / (role + '-chain.pem'), directory / (role + '.key'), directory / 'root-ca.pem'
                commands = []
                def run(*args):
                    commands.append(tuple(map(str, args)))
                    if args[0] == 'test-step':
                        if args[1:5] == ('crypto', 'key', 'format', '--jwk'):
                            return b'{"crv":"P-256","kty":"EC","x":"test-x","y":"test-y"}'
                        return b'not-logged-token'
                    return self.real_run(*args)
                connection = MagicMock()
                response = connection.getresponse.return_value
                response.status = 201
                response.read.return_value = json.dumps({'crt': chain.read_text(), 'ca': root.read_text()}).encode()
                context = MagicMock()
                with patch.object(renewal.pki, 'run', side_effect=run), \
                        patch.object(renewal.http.client, 'HTTPConnection', return_value=connection), \
                        patch.object(renewal.socket, 'create_connection', return_value=MagicMock()), \
                        patch.object(renewal.ssl, 'create_default_context', return_value=context):
                    renewal.request_renewal(self.data, role, chain, key, root, 'test-step')
                    method, endpoint, payload, headers = connection.request.call_args.args
                    self.assertEqual(method, 'POST')
                    if role == 'server':
                        self.assertEqual(endpoint, '/1.0/sign')
                        self.assertEqual(json.loads(payload), {'csr': (directory / 'server.csr').read_text(), 'ott': 'not-logged-token'})
                        self.assertIn('--offline', commands[-1])
                        self.assertIn('evi-csm-renew-evi3-local', commands[-1])
                        self.assertIn('--cnf-file', commands[-1])
                    else:
                        self.assertEqual(endpoint, '/1.0/renew')
                        self.assertIsNone(payload)
                        self.assertEqual(headers, {'Authorization': 'Bearer not-logged-token'})
                        self.assertIn('--renew', commands[-1])
                    self.assertEqual(context.wrap_socket.call_args.kwargs['server_hostname'], self.data['ca_name'])
                    response.status = 401
                    with self.assertRaisesRegex(ValueError, 'HTTP 401'):
                        renewal.request_renewal(self.data, role, chain, key, root, 'test-step')

    def test_corrupt_download_is_never_executed(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            response = io.BytesIO(b'corrupt archive')
            opener = MagicMock()
            opener.open.return_value = response
            with patch.object(renewal, 'INSTALL', directory), patch.object(renewal.https, 'REPO', directory), \
                    patch.object(renewal.platform, 'machine', return_value='x86_64'), \
                    patch.object(renewal.urllib.request, 'build_opener', return_value=opener), \
                    patch.object(renewal.pki, 'run') as run:
                with self.assertRaisesRegex(ValueError, 'SHA-256'):
                    renewal.install_step()
                run.assert_not_called()
                self.assertFalse((directory / 'step').exists())


if __name__ == '__main__':
    unittest.main()
