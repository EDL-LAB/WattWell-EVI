"""Preparation tests use temporary files; no EVI services or network changes."""
import contextlib
import importlib.util
import io
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
SPEC = importlib.util.spec_from_file_location('prepare_https', SCRIPTS / 'prepare-csm-https.py')
prep = importlib.util.module_from_spec(SPEC)
with patch.object(sys, 'path', [str(SCRIPTS)] + sys.path):
    SPEC.loader.exec_module(prep)


class PreparationTests(unittest.TestCase):
    def test_preview_and_invalid_hostname_never_change_system(self):
        for hostname in ('evi3.local', 'EVI3.local', 'evi3.local\ncommand', '../evi3', 'evi3'):
            with self.subTest(hostname=hostname), \
                    patch.object(sys, 'argv', ['prepare', '--hostname', hostname]), \
                    patch.object(prep, 'install_caddy') as install, patch.object(prep, 'prepare_csr') as csr, \
                    contextlib.redirect_stdout(io.StringIO()):
                if hostname == 'evi3.local':
                    prep.main()
                else:
                    with self.assertRaises(ValueError):
                        prep.main()
                install.assert_not_called()
                csr.assert_not_called()

    @unittest.skipUnless(shutil.which('openssl'), 'OpenSSL needed for real CSR verification')
    def test_csr_reuses_key_and_refuses_partial_changed_or_linked_material(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(prep, 'DIRECTORY', Path(temporary) / 'tls'):
            csr = prep.prepare_csr('evi3.local', shutil.which('openssl'))
            key = prep.DIRECTORY / 'server.key'
            original = key.read_bytes(), csr.read_bytes()
            self.assertEqual(key.stat().st_mode & 0o777, 0o600)
            self.assertEqual(prep.DIRECTORY.stat().st_mode & 0o777, 0o700)
            text = subprocess.check_output(['openssl', 'req', '-in', str(csr), '-text', '-noout']).decode()
            self.assertIn('DNS:evi3.local', text)
            self.assertIn('TLS Web Server Authentication', text)
            prep.prepare_csr('evi3.local', shutil.which('openssl'))
            self.assertEqual((key.read_bytes(), csr.read_bytes()), original)
            with self.assertRaisesRegex(RuntimeError, 'another hostname'):
                prep.prepare_csr('evi4.local', shutil.which('openssl'))
            key.chmod(0o644)
            with self.assertRaisesRegex(RuntimeError, '0600'):
                prep.prepare_csr('evi3.local', shutil.which('openssl'))
            key.chmod(0o600)
            subprocess.run(['openssl', 'ecparam', '-name', 'prime256v1', '-genkey', '-noout', '-out', str(key)],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            with self.assertRaisesRegex(RuntimeError, 'do not match'):
                prep.prepare_csr('evi3.local', shutil.which('openssl'))
            key.write_bytes(original[0])
            csr.unlink()
            with self.assertRaisesRegex(RuntimeError, 'part of the TLS identity'):
                prep.prepare_csr('evi3.local', shutil.which('openssl'))
            csr.symlink_to(key)
            with self.assertRaisesRegex(RuntimeError, 'regular file'):
                prep.prepare_csr('evi3.local', shutil.which('openssl'))
            self.assertEqual(key.read_bytes(), original[0])

    def test_installer_extracts_only_expected_binary_and_never_overwrites_other_caddy(self):
        for case in ('new', 'same', 'different', 'symlink', 'bad-version', 'archive-link'):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                target = root / 'bin/caddy'
                target.parent.mkdir()
                fixture = root / 'fixture.tar.gz'
                payload = b'fixture binary'
                with tarfile.open(str(fixture), 'w:gz') as package:
                    member = tarfile.TarInfo('caddy')
                    if case == 'archive-link':
                        member.type = tarfile.SYMTYPE
                        member.linkname = '/etc/shadow'
                        package.addfile(member)
                    else:
                        member.size = len(payload)
                        package.addfile(member, io.BytesIO(payload))
                    extra = tarfile.TarInfo('../../outside')
                    extra.size = 1
                    package.addfile(extra, io.BytesIO(b'x'))
                if case in ('same', 'different'):
                    target.write_bytes(payload if case == 'same' else b'existing')
                if case == 'symlink':
                    target.symlink_to(fixture)
                result = subprocess.CompletedProcess([], 0, 'v' + prep.VERSION + ' test\n' if case != 'bad-version' else 'wrong', '')
                def download(opener, url, path, digest):
                    self.assertEqual(url, prep.URL)
                    self.assertEqual(digest, prep.ARCHIVE_SHA256)
                    shutil.copyfile(fixture, path)
                with patch.object(prep, 'TARGET', target), \
                        patch.object(prep.configure, 'downloader', return_value=object()), \
                        patch.object(prep.configure, 'download', side_effect=download), \
                        patch.object(prep.configure, 'run', return_value=result):
                    if case in ('new', 'same'):
                        prep.install_caddy()
                        self.assertEqual(target.read_bytes(), payload)
                        self.assertEqual(target.stat().st_mode & 0o777, 0o755)
                    else:
                        with self.assertRaises(RuntimeError):
                            prep.install_caddy()
                        if case == 'different':
                            self.assertEqual(target.read_bytes(), b'existing')
                self.assertFalse((root / 'outside').exists())

    def test_apply_checks_root_architecture_and_only_prepares(self):
        status = subprocess.CompletedProcess([], 0, 'armhf\n', '')
        machine = subprocess.CompletedProcess([], 0, 'armv7l\n', '')
        with patch.object(sys, 'argv', ['prepare', '--hostname', 'evi3.local', '--apply']), \
                patch.object(prep.os, 'geteuid', return_value=0), patch.object(prep.os, 'umask'), \
                patch.object(prep.shutil, 'which', return_value='/usr/bin/openssl'), \
                patch.object(prep.configure, 'run', side_effect=[status, machine]) as command, \
                patch.object(prep, 'install_caddy') as install, \
                patch.object(prep, 'prepare_csr', return_value=Path('/test/server.csr')) as csr, \
                contextlib.redirect_stdout(io.StringIO()):
            prep.main()
            install.assert_called_once_with()
            csr.assert_called_once_with('evi3.local', '/usr/bin/openssl')
            self.assertEqual([call.args[0] for call in command.call_args_list], [['dpkg', '--print-architecture'], ['uname', '-m']])
        for uid, arch in ((1000, 'armhf'), (0, 'amd64')):
            with patch.object(sys, 'argv', ['prepare', '--hostname', 'evi3.local', '--apply']), \
                    patch.object(prep.os, 'geteuid', return_value=uid), \
                    patch.object(prep.configure, 'run', return_value=subprocess.CompletedProcess([], 0, arch, '')), \
                    patch.object(prep, 'install_caddy') as install, self.assertRaises(RuntimeError):
                prep.main()
            install.assert_not_called()


if __name__ == '__main__':
    unittest.main()
