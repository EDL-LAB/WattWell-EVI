"""GitHub CLI installation uses temporary paths for user and system destinations."""
import importlib.util
import io
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
SPEC = importlib.util.spec_from_file_location('install_gh', SCRIPTS / 'install-gh.py')
installer = importlib.util.module_from_spec(SPEC)
with patch.object(sys, 'path', [str(SCRIPTS)] + sys.path):
    SPEC.loader.exec_module(installer)


class InstallGhTests(unittest.TestCase):
    def test_installation_and_failures_preserve_existing_files(self):
        for case in ('ok', 'same', 'different', 'symlink', 'root', 'architecture',
                     'download', 'member', 'duplicate', 'binary-symlink', 'version'):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                home = Path(temporary)
                target = home / ('system/bin/gh' if case == 'root' else '.local/bin/gh')
                target.parent.mkdir(parents=True)
                payload = b'authenticated ARM binary fixture'
                if case in ('same', 'different'):
                    target.write_bytes(payload if case == 'same' else b'existing user tool')
                if case == 'symlink':
                    target.symlink_to(home / 'missing')
                calls = []

                def command(args, **kwargs):
                    calls.append(args)
                    if args[0] == 'dpkg':
                        self.assertEqual(args, ['dpkg', '--print-architecture'])
                        output = 'amd64' if case == 'architecture' else 'armhf'
                    else:
                        self.assertEqual(args[1:], ['--version'])
                        self.assertEqual(Path(args[0]).read_bytes(), payload)
                        output = 'gh version ' + ('0.0.0' if case == 'version' else installer.VERSION) + ' (2026-10-26)\n'
                    return subprocess.CompletedProcess(args, 0, output, '')

                def download(opener, url, archive, expected):
                    self.assertEqual(url, installer.URL)
                    self.assertEqual(expected, installer.ARCHIVE_SHA256)
                    if case == 'download':
                        raise ValueError('SHA-256 mismatch')
                    with tarfile.open(str(archive), 'w:gz') as package:
                        member = tarfile.TarInfo(installer.ARCHIVE_NAME + '/bin/' + ('other' if case == 'member' else 'gh'))
                        if case == 'binary-symlink':
                            member.type = tarfile.SYMTYPE
                            member.linkname = '/etc/passwd'
                            package.addfile(member)
                        else:
                            member.size = len(payload)
                            package.addfile(member, io.BytesIO(payload))
                            if case == 'duplicate':
                                package.addfile(member, io.BytesIO(payload))
                        unwanted = tarfile.TarInfo('../unwanted')
                        unwanted.size = 1
                        package.addfile(unwanted, io.BytesIO(b'x'))

                before = target.read_bytes() if target.is_file() else None
                with patch.object(installer.sys, 'argv', ['install-gh.py']), \
                        patch.object(installer.os, 'geteuid', return_value=0 if case == 'root' else 1000), \
                        patch.object(installer, 'Path') as paths, \
                        patch.object(installer.configure, 'run', side_effect=command), \
                        patch.object(installer.configure, 'downloader', return_value=object()), \
                        patch.object(installer.configure, 'download', side_effect=download):
                    paths.side_effect = lambda value: target if str(value) == '/usr/local/bin/gh' else Path(value)
                    paths.home.return_value = home
                    if case in ('ok', 'same', 'root'):
                        installer.main()
                        self.assertEqual(target.read_bytes(), payload)
                        self.assertTrue(target.stat().st_mode & 0o111)
                    else:
                        with self.assertRaises((RuntimeError, ValueError)):
                            installer.main()
                        if before is not None:
                            self.assertEqual(target.read_bytes(), before)
                        else:
                            self.assertFalse(target.exists())
                self.assertEqual(list(target.parent.glob('.gh-install-*')), [])
                self.assertFalse((target.parent / 'unwanted').exists())
                self.assertFalse((home / 'unwanted').exists())
                self.assertTrue(all(args[0] == 'dpkg' or args[1:] == ['--version'] for args in calls))


if __name__ == '__main__':
    unittest.main()
