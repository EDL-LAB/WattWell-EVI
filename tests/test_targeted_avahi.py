"""Targeted installation tests never install packages or modify the host database."""
import hashlib
import importlib.util
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('targeted_configure', Path(__file__).resolve().parents[1] / 'scripts/configure.py')
config = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(config)

STATUS = ('Package: npm\nStatus: install ok installed\nVersion: 1\nArchitecture: all\n'
          'Depends: missing-node-package\nDescription: existing vendor package\n\n'
          'Package: libc6\nStatus: install ok installed\nVersion: 2.28-10\n'
          'Architecture: armhf\nDescription: original libc\n\n')
REPORT = ('The following packages have unmet dependencies:\n'
          ' npm : Depends: missing-node-package but it is not installed\nE: Unmet dependencies.\n')


class TargetedAvahiTests(unittest.TestCase):
    def test_targeted_installation_and_failures_preserve_real_database_and_policy(self):
        cases = ('ok', 'unknown-baseline', 'missing-baseline-record', 'baseline-state',
                 'upgrade', 'missing-avahi', 'bad-hash', 'bad-url', 'url-traversal',
                 'bad-control', 'essential', 'wrong-architecture', 'daemon-all', 'pre-dependency', 'collision', 'dependency-failure',
                 'inventory-race', 'database-race', 'apply-failed', 'unexpected-after')
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                state = root / 'state'
                state.mkdir()
                fs = root / 'fs'
                fs.mkdir()
                status = root / 'real.status'
                before = STATUS.replace('install ok installed', 'install ok unpacked', 1) if case == 'baseline-state' else STATUS
                status.write_text(before)
                policy = root / 'policy'
                policy.write_bytes(b'original policy')
                apt_config = root / 'apt.conf'
                apt_config.write_text('Dir::Etc::main "/dev/null";\n')
                original = {'npm:all': '1', 'libc6:armhf': '2.28-10'}
                version = '0.7-4+deb10u3'
                payload = b'authenticated package fixture'
                digest = hashlib.sha256(payload).hexdigest()
                applied, calls = [], []

                def filesystem(value):
                    if str(value) == '/var/lib/dpkg/status':
                        return status
                    if str(value) == '/':
                        return fs
                    return Path(value)

                def package_state():
                    if case == 'inventory-race':
                        return dict(original, unexpected='1')
                    result = dict(original)
                    if applied:
                        result['avahi-daemon:armhf'] = version
                        if case == 'unexpected-after':
                            result['npm:all'] = '2'
                    return result

                def command(args, check=True, env=None):
                    calls.append(args)
                    output = ''
                    code = 0
                    if args[0] == '/usr/bin/apt-get':
                        planner = Path(env['APT_CONFIG'])
                        self.assertNotEqual(planner, apt_config)
                        self.assertIn('Dir::Cache::pkgcache "";', planner.read_text())
                        planning_status = planner.parent / 'planning.status'
                        records = list(config.paragraphs(planning_status.read_text().splitlines()))
                        self.assertNotIn('Depends', records[0])
                        self.assertEqual(records[0]['Status'], 'install ok installed')
                        self.assertEqual(status.read_text(), STATUS)
                        if '--simulate' in args:
                            output = ('Inst npm [1] (2 Debian [all])' if case == 'upgrade' else
                                      '' if case == 'missing-avahi' else
                                      'Inst avahi-daemon (' + version + ' Debian [armhf])\n')
                        else:
                            self.assertEqual(args, ['/usr/bin/apt-get', '--print-uris', 'download', 'avahi-daemon=' + version])
                            url = ('http://archive.debian.org/debian/pool/a.deb' if case == 'bad-url'
                                   else 'https://archive.debian.org/debian/pool/a.deb')
                            if case == 'url-traversal':
                                url = 'https://archive.debian.org/debian/pool/%2e%2e/a.deb'
                            checksum = 'MD5:bad' if case == 'bad-hash' else 'SHA256:' + digest
                            output = "'{}' a.deb {} {}\n".format(url, len(payload), checksum)
                    elif args[:2] == ['dpkg-deb', '-f']:
                        output = ('Package: avahi-daemon\nVersion: ' + version + '\nArchitecture: armhf\n'
                                  'Depends: libc6 (>= 2.28)\nDescription: new Avahi\n')
                        if case == 'bad-control':
                            output = output.replace('Package: avahi-daemon', 'Package: unexpected')
                        if case == 'pre-dependency':
                            output += 'Pre-Depends: special-package\n'
                        if case == 'essential':
                            output += 'Essential: yes\n'
                        if case == 'wrong-architecture':
                            output = output.replace('Architecture: armhf', 'Architecture: arm64')
                        if case == 'daemon-all':
                            output = output.replace('Architecture: armhf', 'Architecture: all')
                    elif args[:2] == ['dpkg-deb', '-x']:
                        extracted = Path(args[3])
                        (extracted / 'usr/share').mkdir(parents=True)
                        (extracted / 'usr/share/avahi-fixture').write_text('new file')
                        if case == 'collision':
                            (fs / 'usr/share').mkdir(parents=True)
                            (fs / 'usr/share/avahi-fixture').write_text('vendor data')
                    elif args[0] == 'dpkg' and '--simulate' in args:
                        if '--configure' in args:
                            private = Path(next(a.split('=', 1)[1] for a in args if a.startswith('--admindir=')))
                            actual = list(config.paragraphs((private / 'status').read_text().splitlines()))
                            self.assertEqual(actual[0]['Depends'], 'missing-node-package')
                            self.assertEqual(actual[-1]['Status'], 'install ok unpacked')
                            if case == 'dependency-failure':
                                raise RuntimeError('new dependency missing')
                        elif case == 'database-race':
                            status.write_text(STATUS + '\n')
                    else:
                        self.assertEqual(args[:3], ['dpkg', '--log=' + str(state / 'avahi-dpkg.log'), '--install'])
                        self.assertNotIn('--simulate', args)
                        self.assertFalse(any(a.startswith(('--force', '--admindir', '--ignore')) for a in args))
                        self.assertFalse(check)
                        self.assertEqual(env['APT_CONFIG'], str(apt_config))
                        self.assertEqual(policy.read_bytes(), config.DENY)
                        applied.append(True)
                        code = int(case == 'apply-failed')
                        output = 'dpkg installation output'
                    return subprocess.CompletedProcess(args, code, output, '')

                def fetch(opener, url, destination, expected):
                    self.assertEqual(expected, digest)
                    Path(destination).write_bytes(payload)

                with patch.object(config, 'STATE', state), patch.object(config, 'POLICY', policy), \
                        patch.object(config, 'Path', filesystem), patch.object(config, 'run', command), \
                        patch.object(config, 'inventory', package_state), patch.object(config, 'download', fetch), \
                        patch.object(config, 'downloader', return_value=object()), patch.object(config, 'check_dpkg_hooks'):
                    report = REPORT.replace(' npm :', ' other :') if case == 'unknown-baseline' else REPORT
                    if case == 'missing-baseline-record':
                        report = REPORT.replace(' npm :', ' librsync-dev :')
                    env = {'APT_CONFIG': str(apt_config)}
                    arguments = ['/usr/bin/apt-get', '--no-remove', '--no-upgrade', 'install', 'avahi-daemon']
                    if case == 'ok':
                        config.install_avahi_targeted(original, env, report, arguments)
                    else:
                        with self.assertRaises(RuntimeError):
                            config.install_avahi_targeted(original, env, report, arguments)
                self.assertEqual(bool(applied), case in ('ok', 'apply-failed', 'unexpected-after'))
                self.assertEqual(policy.read_bytes(), b'original policy')
                if case != 'database-race':
                    self.assertEqual(status.read_text(), before)
                self.assertEqual((state / 'apt-baseline-check.txt').read_text(), report)
                if applied:
                    self.assertEqual((state / 'avahi-dpkg-output.txt').read_text(), 'dpkg installation output')
                    self.assertEqual((state / 'avahi-status-before.txt').read_text(), STATUS)

    def test_installer_uses_targeted_path_when_baseline_check_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def command(args, **kwargs):
                if args == ['/usr/bin/apt-get', 'update']:
                    return subprocess.CompletedProcess(args, 0, '', '')
                self.assertEqual(args, ['/usr/bin/apt-get', 'check'])
                return subprocess.CompletedProcess(args, 100, REPORT, '')

            with patch.object(config, 'STATE', root / 'state'), patch.object(config, 'APT_STATE', root / 'apt'), \
                    patch.object(config, 'run', command), patch.object(config.os, 'chown'), \
                    patch.object(config, 'install_avahi_targeted', side_effect=RuntimeError('targeted-stop')) as targeted:
                with self.assertRaisesRegex(RuntimeError, 'targeted-stop'):
                    config.install_avahi({'npm:all': '1'}, 'evi3')
                self.assertEqual(targeted.call_args[0][0], {'npm:all': '1'})
                self.assertEqual(targeted.call_args[0][2], REPORT)

    def test_payload_checks_preserve_existing_files_and_broken_symlinks(self):
        for collision in ('file', 'broken-symlink', 'directory-blocked'):
            with self.subTest(collision=collision), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                extracted, live = root / 'package', root / 'live'
                (extracted / 'usr/share/new-dir').mkdir(parents=True)
                (extracted / 'usr/share/new-file').write_text('new')
                (live / 'usr/share').mkdir(parents=True)
                target = live / ('usr/share/new-dir' if collision == 'directory-blocked' else 'usr/share/new-file')
                if collision == 'broken-symlink':
                    target.symlink_to('missing-vendor-file')
                else:
                    target.write_text('vendor file')
                with patch.object(config, 'Path', return_value=live):
                    with self.assertRaises(RuntimeError):
                        config.check_new_payload(extracted)
                self.assertTrue(target.is_symlink() if collision == 'broken-symlink' else target.read_text() == 'vendor file')

    def test_control_roundtrip_preserves_multiline_fields(self):
        text = STATUS + 'Package: extra\nDescription: first line\n second line\n .\n last line\n\n'
        records = list(config.paragraphs(text.splitlines()))
        self.assertEqual(list(config.paragraphs(config.control_text(records).splitlines())), records)

    @unittest.skipUnless(shutil.which('dpkg'), 'dpkg is needed for the isolated simulation check')
    def test_real_dpkg_configuration_simulation_checks_only_requested_dependencies(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / 'db'
            db.mkdir()
            base = ('Package: vendor-broken\nStatus: install ok installed\nVersion: 1\nArchitecture: all\n'
                    'Maintainer: Test <test@example.invalid>\nDepends: vendor-missing\nDescription: baseline\n\n')
            new = ('Package: avahi-fixture\nStatus: install ok unpacked\nVersion: 1\nArchitecture: all\n'
                   'Maintainer: Test <test@example.invalid>\nDepends: helper-fixture\nDescription: new fixture\n\n')
            helper = ('Package: helper-fixture\nStatus: install ok unpacked\nVersion: 1\nArchitecture: all\n'
                      'Maintainer: Test <test@example.invalid>\nDescription: helper\n\n')
            for complete in (False, True):
                text = base + new + (helper if complete else '')
                (db / 'status').write_text(text)
                arguments = ['dpkg', '--admindir=' + str(db), '--log=' + str(root / 'log'),
                             '--no-triggers', '--simulate', '--configure']
                result = subprocess.run(arguments + (['helper-fixture'] if complete else []) + ['avahi-fixture'],
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
                self.assertEqual(result.returncode, 0 if complete else 1, result.stderr)
                self.assertEqual((db / 'status').read_text(), text)


if __name__ == '__main__':
    unittest.main()
