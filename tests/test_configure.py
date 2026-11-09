"""No hardware or real /etc writes: configuration tests use temporary files."""
import hashlib
import importlib.util
import io
import json
import lzma
from pathlib import Path
import stat
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import DEFAULT, patch

SPEC = importlib.util.spec_from_file_location('configure', Path(__file__).resolve().parents[1] / 'scripts/configure.py')
config = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(config)

HOSTS = ('127.0.0.1\tlocalhost\n::1 localhost ip6-localhost ip6-loopback\n'
         'ff02::1 ip6-allnodes\nff02::2 ip6-allrouters\n\n'
         '127.0.1.1 dhsom dhsom elbe-daemon # keep this comment\n')


class ConfigurationTests(unittest.TestCase):
    def test_normal_configuration_installs_gh_but_repair_planning_does_not(self):
        for planning in (False, True):
            with self.subTest(planning=planning), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                for name, data in (('etc/os-release', 'ID=debian\nVERSION_ID="10"\n'),
                                   ('etc/hostname', 'evi3\n'),
                                   ('usr/local/etc/distro_version', 'ww-distro-dh-v1.3.27\n'),
                                   ('keyring', 'fixture')):
                    path = root / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(data)
                calls = []

                def filesystem(value):
                    path = str(value)
                    return root / path.lstrip('/') if path.startswith(('/etc/', '/usr/local/etc/', '/var/lib/dpkg/')) else Path(value)

                def command(args, **kwargs):
                    calls.append(args)
                    if args == ['dpkg', '--print-architecture']:
                        return subprocess.CompletedProcess(args, 0, 'armhf\n', '')
                    self.assertEqual(args, ['/usr/bin/python3', str(config.PROJECT / 'scripts/install-gh.py')])
                    helpers['install_avahi'].assert_called_once()
                    return subprocess.CompletedProcess(args, 0, 'GitHub CLI installed\n', '')

                with patch.object(config.sys, 'argv', ['configure.py'] + (['--plan-package-repair'] if planning else [])), \
                        patch.dict(config.os.environ), patch.object(config.os, 'geteuid', return_value=0), \
                        patch.object(config.os, 'umask'), patch.object(config.os, 'access', return_value=True), \
                        patch.object(config.shutil, 'which', return_value='/usr/bin/tool'), \
                        patch.object(config, 'Path', side_effect=filesystem), \
                        patch.object(config, 'STATE', root / 'state'), patch.object(config, 'KEYRING', root / 'keyring'), \
                        patch.object(config, 'run', side_effect=command), patch('builtins.input', return_value=''), \
                        patch.object(config, 'inventory', return_value={'avahi-daemon:armhf': '1', 'libnss-mdns:armhf': '0.14.1-1'}), \
                        patch.object(config, 'apt_ready', return_value=True), \
                        patch.multiple(config, restore_policy=DEFAULT, plan_package_repair=DEFAULT, configure_name=DEFAULT,
                                       configure_dhcp=DEFAULT, downloader=DEFAULT, restore_apt=DEFAULT, install_avahi=DEFAULT) as helpers:
                    config.main()
                    self.assertEqual(helpers['plan_package_repair'].call_count, int(planning))
                    self.assertEqual(helpers['restore_policy'].call_count, int(not planning))
                self.assertEqual(len(calls), 1 if planning else 2)

    def test_initial_inventory_repairs_only_the_known_libc_bin_state(self):
        row = 'libc-bin\tarmhf\t2.28-10\tinstall ok half-configured\n'
        healthy = row.replace('half-configured', 'installed')
        for case in ('ok', 'other-incomplete', 'different-version', 'after-changed', 'not-initial'):
            with self.subTest(case=case):
                before = row
                if case == 'other-incomplete':
                    before += 'dbus\tarmhf\t1\tinstall ok unpacked\n'
                if case == 'different-version':
                    before = before.replace('2.28-10', '2.28-11')
                after = healthy + ('extra\tarmhf\t1\tinstall ok installed\n' if case == 'after-changed' else '')
                results = [subprocess.CompletedProcess([], 0, value, '') for value in (before, after)]
                with patch.object(config, 'run', side_effect=results), \
                        patch.object(config, 'configure_libc_bin') as repair, \
                        patch.object(config, 'package_failure_report', return_value=Path('/report')):
                    if case == 'ok':
                        self.assertEqual(config.inventory(repair_libc=True), {'libc-bin:armhf': '2.28-10'})
                    else:
                        with self.assertRaises(RuntimeError):
                            config.inventory(repair_libc=case != 'not-initial')
                    self.assertEqual(repair.call_count, int(case in ('ok', 'after-changed')))

    def test_targeted_libc_configuration_guards_logs_backups_and_policy(self):
        fixture = Path(__file__).parent / 'fixtures/libc-bin-2.28-10.postinst'
        for case in ('ok', 'known-hook', 'known-hook-extra', 'known-hook-modified',
                     'script-changed', 'version', 'custom-nss', 'hook',
                     'scan-failed', 'configure-failed', 'nss-changed'):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                state = root / 'state'
                state.mkdir()
                policy = root / 'policy'
                policy.write_bytes(b'original policy')
                files = {
                    '/var/lib/dpkg/info/libc-bin.postinst': fixture.read_bytes(),
                    '/etc/nsswitch.conf': b'hosts: files dns\n',
                    '/usr/share/libc-bin/nsswitch.conf': b'hosts: files dns\n',
                    '/etc/dpkg/dpkg.cfg': b'# no hooks\n',
                    '/var/lib/dpkg/status': b'original database',
                    '/etc/ld.so.cache': b'original cache',
                }
                if case == 'script-changed':
                    files['/var/lib/dpkg/info/libc-bin.postinst'] += b'false\n'
                if case == 'custom-nss':
                    files['/etc/nsswitch.conf'] += b'# vendor custom\n'
                if case == 'hook':
                    files['/etc/dpkg/dpkg.cfg'] = b'post-invoke=systemctl restart critical.service\n'
                if case.startswith('known-hook'):
                    hook = config.PKG_CONFIG_ARCH_HOOK + '\n'
                    if case == 'known-hook-extra':
                        hook += 'post-invoke=systemctl restart critical.service\n'
                    if case == 'known-hook-modified':
                        hook = hook.replace('add-architecture', 'configure')
                    files['/etc/dpkg/dpkg.cfg.d/pkg-config-hook-config'] = hook.encode()
                for name, data in files.items():
                    path = root / name.lstrip('/')
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(data)
                calls = []

                def filesystem(value):
                    path = str(value)
                    if path.startswith(('/etc/', '/var/lib/dpkg/', '/usr/share/libc-bin/', '/var/cache/ldconfig/', '/root/')):
                        return root / path.lstrip('/')
                    return Path(value)

                def command(args, check=True, env=None):
                    calls.append(args)
                    if args[0] == 'dpkg-query':
                        self.assertEqual(args, ['dpkg-query', '-W', '-f=${Config-Version}', 'libc-bin'])
                        return subprocess.CompletedProcess(args, 0, 'old' if case == 'version' else '2.28-10', '')
                    if args[0] == '/sbin/ldconfig':
                        self.assertEqual(args, ['/sbin/ldconfig', '-N', '-X'])
                        if case == 'scan-failed':
                            raise RuntimeError('scan failed')
                        return subprocess.CompletedProcess(args, 0, '', '')
                    self.assertEqual(args, ['dpkg', '--no-triggers', '--log=' + str(state / 'libc-bin-dpkg.log'),
                                            '--configure', 'libc-bin'])
                    self.assertFalse(check)
                    self.assertEqual(policy.read_bytes(), config.DENY)
                    self.assertEqual(env['PATH'], '/usr/sbin:/usr/bin:/sbin:/bin')
                    self.assertNotIn('DPKG_FORCE', env)
                    self.assertNotIn('DPKG_FRONTEND_LOCKED', env)
                    if case == 'nss-changed':
                        filesystem('/etc/nsswitch.conf').write_bytes(b'concurrent change')
                    return subprocess.CompletedProcess(args, int(case == 'configure-failed'), 'configure output\n', 'configure diagnostic\n')

                with patch.object(config, 'STATE', state), patch.object(config, 'POLICY', policy), \
                        patch.object(config, 'Path', filesystem), patch.object(config, 'run', command), \
                        patch.object(config.os.path, 'expanduser', return_value='/root/.dpkg.cfg'):
                    if case in ('ok', 'known-hook'):
                        config.configure_libc_bin()
                    else:
                        with self.assertRaises(RuntimeError):
                            config.configure_libc_bin()
                applied = any(args[0] == 'dpkg' for args in calls)
                self.assertEqual(applied, case in ('ok', 'known-hook', 'configure-failed', 'nss-changed'))
                self.assertEqual(policy.read_bytes(), b'original policy')
                self.assertFalse((state / 'policy-journal.json').exists())
                if applied:
                    self.assertIn('configure diagnostic', (state / 'libc-bin-configure.log').read_text())
                    self.assertEqual((state / 'backups/etc/ld.so.cache').read_bytes(), b'original cache')
                    self.assertEqual((state / 'backups/var/lib/dpkg/status').read_bytes(), b'original database')

    def test_reviewed_pkg_config_hook_runs_only_for_architecture_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            hook = root / 'pkg-config-dpkghook'
            hook.write_text('#!/bin/sh\nprintf "%s\\n" "$1" >> "$TRACE"\n')
            hook.chmod(0o755)
            trace = root / 'trace'
            command = config.PKG_CONFIG_ARCH_HOOK.split('=', 1)[1]
            self.assertEqual(command.count('/usr/share/pkg-config-dpkghook'), 2)
            command = command.replace('/usr/share/pkg-config-dpkghook', str(hook))
            for action in ('configure', 'install', 'triggers-only', 'add-architecture', 'remove-architecture'):
                with self.subTest(action=action):
                    if trace.exists():
                        trace.unlink()
                    result = subprocess.run(['/bin/sh', '-c', command],
                                            env={'DPKG_HOOK_ACTION': action, 'TRACE': str(trace)},
                                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    if action in ('add-architecture', 'remove-architecture'):
                        self.assertEqual(trace.read_text(), 'update\n')
                    else:
                        self.assertFalse(trace.exists())

    def test_incomplete_package_collects_read_only_diagnostics_and_still_blocks(self):
        text = ('libc-bin\tarmhf\t2.28-10\tinstall ok half-configured\n'
                'apt\tarmhf\t1.8.2.1\tinstall ok installed\n')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / 'state'
            state.mkdir()
            postinst = root / 'libc-bin.postinst'
            postinst.write_text('#!/bin/sh\nldconfig\n')
            log = root / 'term.log'
            log.write_bytes(b'old line\n' * 4000 + b'libc-bin: ldconfig: not found\n')
            calls = []

            def filesystem(value):
                if str(value) == '/var/lib/dpkg/info/libc-bin.postinst':
                    return postinst
                if str(value) == '/var/log/apt/term.log':
                    return log
                if str(value).startswith(('/var/lib/dpkg/info/', '/var/log/', '/sbin/', '/usr/sbin/')):
                    return root / 'missing'
                return Path(value)

            def command(args, **kwargs):
                calls.append(args)
                if args[0] == 'dpkg-query':
                    return subprocess.CompletedProcess(args, 0, text, '')
                self.assertEqual(args, ['dpkg', '--audit'])
                self.assertFalse(kwargs['check'])
                return subprocess.CompletedProcess(args, 1, 'libc-bin requires configuration\n', 'audit warning\n')

            with patch.object(config, 'STATE', state), patch.object(config, 'Path', filesystem), \
                    patch.object(config, 'run', command), patch.object(config.os, 'access', return_value=False):
                with self.assertRaisesRegex(RuntimeError, 'half-configured') as failure:
                    config.inventory()
                report = state / 'package-state-report.txt'
                self.assertIn(str(report), str(failure.exception))
                contents = report.read_text()
                for expected in ('libc-bin\tarmhf\t2.28-10', '#!/bin/sh\nldconfig',
                                 'ldconfig: not found', 'audit warning', 'Unavailable:', '16384 bytes'):
                    self.assertIn(expected, contents)
                self.assertNotIn('apt\tarmhf', contents)
                self.assertLess(len(contents), 19000)
                self.assertEqual(stat.S_IMODE(report.stat().st_mode), 0o600)
                self.assertEqual(len(calls), 2)
                with patch.object(config, 'replace_file', side_effect=OSError('read-only state')):
                    with self.assertRaisesRegex(RuntimeError, 'half-configured.*\nDiagnostic collection also failed'):
                        config.inventory()
            self.assertEqual(postinst.read_text(), '#!/bin/sh\nldconfig\n')

    def test_healthy_inventory_never_collects_failure_report(self):
        result = subprocess.CompletedProcess([], 0, 'apt\tarmhf\t1.8.2.1\tinstall ok installed\n', '')
        with patch.object(config, 'run', return_value=result), patch.object(config, 'package_failure_report') as diagnostic:
            self.assertEqual(config.inventory(), {'apt:armhf': '1.8.2.1'})
            diagnostic.assert_not_called()

    def test_avahi_installation_guard_and_failure_restore_policy(self):
        for case in ('ok', 'both-missing', 'mdns-only', 'mdns-not-planned', 'mdns-missing-after', 'upgrade', 'inventory-race', 'apply-failed'):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                state = root / 'state'
                state.mkdir()
                policy = root / 'policy'
                original_policy = b'#!/bin/sh\nexit 42\n'
                policy.write_bytes(original_policy)
                avahi = root / 'avahi.conf'
                mdns_only = case.startswith('mdns-')
                original = {'libc6:armhf': '2.28-10'}
                if mdns_only:
                    original['avahi-daemon:armhf'] = '0.7-4'
                    requested = ['libnss-mdns']
                elif case == 'both-missing':
                    requested = ['avahi-daemon', 'libnss-mdns']
                else:
                    original['libnss-mdns:armhf'] = '0.14.1-1'
                    requested = ['avahi-daemon']
                nsswitch = root / 'nsswitch.conf'
                nsswitch.write_text('hosts: files mdns4_minimal [NOTFOUND=return] dns\n')
                calls = []
                applied = []
                active = []

                def filesystem(value):
                    if str(value) == '/etc/avahi/avahi-daemon.conf': return avahi
                    if str(value) == '/etc/nsswitch.conf': return nsswitch
                    return Path(value)

                def package_state():
                    if case == 'inventory-race':
                        return dict(original, unexpected='1')
                    if applied and case != 'mdns-missing-after':
                        return dict(original, **{package + ':armhf': '0.7-4' for package in requested})
                    return original

                def command(args, check=True, env=None):
                    calls.append((args, env))
                    text = ''
                    code = 0
                    if '--simulate' in args:
                        if case == 'upgrade':
                            text = 'Inst libc6 [2.28-10] (2.28-11 Debian:10 [armhf])'
                        elif case != 'mdns-not-planned':
                            text = '\n'.join('Inst ' + package + ' (0.7-4 Debian:10 [armhf])' for package in requested)
                    if '--assume-yes' in args:
                        self.assertEqual(args[args.index('install') + 1:], requested)
                        self.assertIn('--no-upgrade', args)
                        self.assertIn('--no-remove', args)
                        self.assertEqual(policy.read_bytes(), config.DENY)
                        if case == 'apply-failed':
                            raise RuntimeError('package failure')
                        applied.append(True)
                    if args[:2] == ['systemctl', 'is-active'] and args[-1] == 'avahi-daemon.service':
                        code = 0 if active else 3
                    if args == ['systemctl', 'start', 'avahi-daemon.service']:
                        active.append(True)
                    return subprocess.CompletedProcess(args, code, text, '')

                user = SimpleNamespace(pw_uid=0, pw_gid=0, pw_name='test-sandbox')
                with patch.object(config, 'STATE', state), patch.object(config, 'APT_STATE', root / 'apt'), \
                        patch.object(config, 'POLICY', policy), patch.object(config, 'Path', filesystem), \
                        patch.object(config, 'inventory', package_state), patch.object(config, 'run', command), \
                        patch.object(config.pwd, 'getpwnam', return_value=user), patch.object(config.os, 'chown'):
                    if case in ('ok', 'both-missing', 'mdns-only'):
                        config.install_avahi(original, 'evi3')
                        apt_calls = [call for call in calls if call[0][0] == '/usr/bin/apt-get']
                        config.install_avahi(package_state(), 'evi3')
                        self.assertEqual([call for call in calls if call[0][0] == '/usr/bin/apt-get'], apt_calls)
                    else:
                        with self.assertRaises(RuntimeError):
                            config.install_avahi(original, 'evi3')
                self.assertEqual(policy.read_bytes(), original_policy)
                self.assertEqual(bool(applied), case in ('ok', 'both-missing', 'mdns-only', 'mdns-missing-after'))
                self.assertFalse((state / 'policy-journal.json').exists())
                if case in ('ok', 'both-missing', 'mdns-only'):
                    self.assertIn('allow-interfaces=eth1', avahi.read_text())
                    if 'libnss-mdns' in requested:
                        self.assertEqual((state / 'nsswitch-before-mdns.conf').read_bytes(), nsswitch.read_bytes())
                    configuration = (root / 'apt/apt.conf').read_text()
                    self.assertIn('Dir::Etc::main "/dev/null"', configuration)
                    for option in ('parts', 'sourceparts', 'preferencesparts', 'trustedparts'):
                        self.assertIn('Dir::Etc::{} "{}"'.format(option, root / 'apt/empty'), configuration)
                    self.assertEqual(list((root / 'apt/empty').iterdir()), [])
                    for cache in ('pkgcache', 'srcpkgcache'):
                        self.assertIn('Dir::Cache::{} "{}"'.format(cache, root / 'apt' / (cache + '.bin')), configuration)
                    self.assertNotIn('"-"', configuration)
                    self.assertNotIn('AllowUnauthenticated', configuration)
                    self.assertNotIn((['systemctl', 'restart', 'dbus.service'], None), calls)
                    self.assertTrue(active)

    def test_not_installed_package_records_are_not_incomplete_installations(self):
        absent = 'libnss-mdns\t\t\tunknown ok not-installed\n'
        present = 'avahi-daemon\tarmhf\t0.7-4\tinstall ok installed\n'
        self.assertEqual(config.parse_packages(present + absent), {'avahi-daemon:armhf': '0.7-4'})
        with self.assertRaises(RuntimeError):
            config.parse_packages(present + absent.replace('unknown ok not-installed', 'unknown reinstreq not-installed'))

    def test_mdns_preflight_stops_before_package_changes(self):
        for case in ('missing-perl', 'linked-nss'):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                target = root / 'original-nss'
                target.write_text('hosts: files dns\n')
                nss = root / 'nsswitch.conf'
                if case == 'linked-nss': nss.symlink_to(target)
                def filesystem(value):
                    return nss if str(value) == '/etc/nsswitch.conf' else Path(value)
                with patch.object(config, 'Path', side_effect=filesystem), patch.object(config, 'STATE', root / 'state'), \
                        patch.object(config, 'APT_STATE', root / 'apt'), patch.object(config.shutil, 'which', return_value=None if case == 'missing-perl' else '/usr/bin/perl'), \
                        patch.object(config, 'run') as command, self.assertRaises(RuntimeError):
                    config.install_avahi({'avahi-daemon:armhf': '0.7-4'}, 'evi3')
                command.assert_not_called()
                self.assertEqual(target.read_text(), 'hosts: files dns\n')

    def test_apt_requires_executable_signature_helper_even_if_apt_get_works(self):
        required = ('/usr/bin/apt-get', '/usr/bin/apt-config', '/usr/bin/apt-cache',
                    '/usr/bin/apt-key', '/usr/lib/apt/methods/http',
                    '/usr/lib/apt/methods/https', '/usr/lib/apt/methods/gpgv')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in required:
                path = root / name.lstrip('/')
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('#!/bin/sh\nexit 0\n')
                path.chmod(0o755)

            def filesystem(value):
                return root / str(value).lstrip('/')

            def executable(value, mode):
                self.assertEqual(mode, config.os.X_OK)
                return bool(filesystem(value).stat().st_mode & stat.S_IXUSR)

            with patch.object(config, 'Path', filesystem), patch.object(config.os, 'access', executable), \
                    patch.object(config, 'run', return_value=subprocess.CompletedProcess([], 0, 'apt 1.8.2.1', '')) as command:
                self.assertTrue(config.apt_ready())
                command.reset_mock()
                helper = filesystem('/usr/bin/apt-key')
                helper.chmod(0o644)
                self.assertFalse(config.apt_ready())
                command.assert_not_called()
                helper.unlink()
                self.assertFalse(config.apt_ready())
                command.assert_not_called()

    def test_apt_recovery_rechecks_runtime_and_skips_complete_installation(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(config, 'STATE', Path(directory)), \
                patch.object(config, 'authenticated_apt_packages', return_value={}) as fetch:
            with patch.object(config, 'apt_ready', return_value=True):
                config.restore_apt(None, {})
                fetch.assert_not_called()
            with patch.object(config, 'apt_ready', return_value=False):
                with self.assertRaisesRegex(RuntimeError, 'still incomplete'):
                    config.restore_apt(None, {'apt:armhf': '1.8.2.1', 'libapt-pkg5.0:armhf': '1.8.2.1'})
                fetch.assert_called_once()

    def test_apt_rejects_nonempty_or_symlinked_isolation_directory(self):
        for symlink in (False, True):
            with self.subTest(symlink=symlink), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                apt = root / 'apt'
                apt.mkdir()
                target = root / 'target'
                target.mkdir()
                if symlink:
                    (apt / 'empty').symlink_to(target, target_is_directory=True)
                else:
                    (apt / 'empty').mkdir()
                    (apt / 'empty/vendor.conf').write_text('DPkg::Post-Invoke {"false";};')
                with patch.object(config, 'APT_STATE', apt), patch.object(config, 'run') as command:
                    with self.assertRaisesRegex(RuntimeError, 'isolated APT empty directory'):
                        config.install_avahi({}, 'evi3')
                    command.assert_not_called()

    def test_apt_recovery_never_falls_back_to_another_version(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(config, 'STATE', Path(directory)), \
                patch.object(config, 'apt_ready', return_value=False), \
                patch.object(config, 'authenticated_apt_packages', side_effect=RuntimeError('snapshot offline')) as fetch, \
                patch.object(config, 'replace_file') as write:
            with self.assertRaisesRegex(RuntimeError, 'registered version'):
                config.restore_apt(None, {'apt:armhf': '1.8.2.3', 'libapt-pkg5.0:armhf': '1.8.2.3'})
            fetch.assert_not_called()
            with self.assertRaisesRegex(RuntimeError, 'snapshot offline'):
                config.restore_apt(None, {'apt:armhf': '1.8.2.1', 'libapt-pkg5.0:armhf': '1.8.2.1'})
            write.assert_not_called()

    def test_hostname_transaction_rolls_back_when_apply_or_resolution_fails(self):
        for failure in ('hostname', 'getent'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                etc = root / 'etc'
                etc.mkdir()
                (etc / 'hosts').write_text(HOSTS)
                (etc / 'hostname').write_text('dhsom\n')
                kernel = ['dhsom']

                def filesystem(value):
                    return etc / Path(value).name if str(value) in ('/etc/hosts', '/etc/hostname') else Path(value)

                def command(args, **kwargs):
                    if args[0] == 'hostname':
                        if len(args) == 2:
                            kernel[0] = args[1]
                            if args[1] == 'evi3' and failure == 'hostname':
                                raise RuntimeError('hostname failed')
                        return subprocess.CompletedProcess(args, 0, kernel[0] + '\n', '')
                    if args[0] == 'getent':
                        raise RuntimeError('getent failed')
                    raise AssertionError(args)

                with patch.object(config, 'STATE', root / 'state'), patch.object(config, 'Path', filesystem), patch.object(config, 'run', command):
                    with self.assertRaises(RuntimeError):
                        config.configure_name('evi3')
                self.assertEqual((etc / 'hosts').read_text(), HOSTS)
                self.assertEqual((etc / 'hostname').read_text(), 'dhsom\n')
                self.assertEqual(kernel[0], 'dhsom')

    def test_hostname_validation(self):
        for name in ('evi3', 'evi-lab-2', 'a', 'x' * 63):
            self.assertTrue(config.valid_hostname(name), name)
        for name in ('', '-evi', 'evi-', 'evi.local', 'UPPER', 'x' * 64,
                     'name\nother', '$(touch /tmp/unsafe)', 'a b'):
            self.assertFalse(config.valid_hostname(name), name)

    def test_hosts_preserve_aliases_ipv6_and_comments_and_repeat(self):
        expected = HOSTS.replace('dhsom dhsom elbe-daemon', 'evi3 dhsom elbe-daemon')
        self.assertEqual(config.hosts_with_name(HOSTS, 'evi3'), expected)
        self.assertEqual(config.hosts_with_name(expected, 'evi3'), expected)
        added = config.hosts_with_name('127.0.0.1 localhost', 'new-evi')
        self.assertTrue(added.endswith('\n127.0.1.1 new-evi\n'))
        with self.assertRaises(ValueError):
            config.hosts_with_name(HOSTS + '127.0.1.1 other\n', 'evi3')

    def test_atomic_backup_and_symlink_refusal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / 'hosts'
            target.write_bytes(b'original')
            target.chmod(0o640)
            with patch.object(config, 'STATE', root / 'state'):
                self.assertTrue(config.replace_file(target, b'first'))
                self.assertFalse(config.replace_file(target, b'first'))
                self.assertTrue(config.replace_file(target, b'second'))
                saved = config.STATE / 'backups' / str(target).lstrip('/')
                self.assertEqual(saved.read_bytes(), b'original')
                self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o640)
                link = root / 'link'
                link.symlink_to(target)
                with self.assertRaises(RuntimeError):
                    config.replace_file(link, b'unsafe')
                with patch.object(config.os, 'replace', side_effect=OSError('read-only')):
                    with self.assertRaises(OSError):
                        config.replace_file(target, b'failed')
                self.assertEqual(target.read_bytes(), b'second')
                self.assertFalse(list(root.glob('.hosts-*')))

    def test_static_addresses_exclude_dynamic_and_other_interfaces(self):
        data = ('5: eth1    inet 192.168.137.100/24 brd 192.168.137.255 scope global eth1:0\\'
                '       valid_lft forever preferred_lft forever\n'
                '5: eth1    inet 192.168.200.11/24 scope global secondary eth1\\'
                '       valid_lft forever preferred_lft forever\n'
                '5: eth1    inet 192.168.15.106/24 scope global dynamic eth1\\'
                '       valid_lft 43140sec preferred_lft 43140sec\n'
                '5: eth1    inet 192.168.15.107/24 scope global dynamic eth1\\'
                '       valid_lft forever preferred_lft forever\n')
        expected = [('192.168.137.100/24', 'eth1:0'), ('192.168.200.11/24', 'eth1')]
        self.assertEqual(config.static_addresses(data), expected)
        self.assertEqual(config.static_addresses(data + data), expected)
        self.assertEqual(config.static_addresses(''), [])
        with self.assertRaises(ValueError):
            config.static_addresses(data.replace('5: eth1 ', '4: qca0 '))

    def test_static_addresses_reject_missing_or_malformed_fields(self):
        valid = ('5: eth1 inet 192.168.200.11/24 scope global eth1\\'
                 ' valid_lft forever preferred_lft forever\n')
        for data in ('[{}]', valid.replace('valid_lft forever', ''),
                     valid.replace('scope global', ''), valid.replace('valid_lft forever', 'valid_lft'),
                     valid.replace('forever', 'unknown'), valid.replace('192.168.200.11/24', 'invalid/24'),
                     valid.replace('192.168.200.11/24', '192.168.200.11'), valid.replace('global eth1', 'global qca0'),
                     valid.replace('scope global', 'scope global scope global')):
            with self.subTest(data=data), self.assertRaises(ValueError):
                config.static_addresses(data)

    def test_dhcp_saves_one_line_inventory_and_refuses_unknown_format_before_writes(self):
        for malformed in (False, True):
            with self.subTest(malformed=malformed), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                state = root / 'state'
                unit = root / 'wattwell-eth1-dhcp.service'
                helper = root / 'helper'
                calls = []

                def filesystem(value):
                    if str(value) == '/etc/systemd/system/wattwell-eth1-dhcp.service':
                        return unit
                    if str(value) == '/usr/local/lib/wattwell-evi':
                        return helper
                    return Path(value)

                def command(args, **kwargs):
                    calls.append(args)
                    if args[0] == 'ip':
                        self.assertEqual(args, ['ip', '-4', '-o', 'addr', 'show', 'dev', 'eth1'])
                        text = '[{}]' if malformed else ('5: eth1 inet 192.168.200.11/24 scope global eth1\\'
                                                        ' valid_lft forever preferred_lft forever\n')
                    else:
                        self.assertTrue((state / 'static-addresses').is_file())
                        text = 'evi3\n' if args == ['hostname'] else ''
                    return subprocess.CompletedProcess(args, 0, text, '')

                with patch.object(config, 'STATE', state), patch.object(config, 'Path', filesystem), \
                        patch.object(config, 'run', command):
                    if malformed:
                        with self.assertRaises(ValueError):
                            config.configure_dhcp('evi3')
                        self.assertEqual(len(calls), 1)
                        self.assertFalse(state.exists())
                        self.assertFalse(unit.exists())
                    else:
                        config.configure_dhcp('evi3')
                        self.assertEqual((state / 'static-addresses').read_text(), '192.168.200.11/24 eth1\n')
                        self.assertTrue(unit.is_file())

    def test_avahi_preserves_other_settings_and_is_idempotent(self):
        original = ('# custom\n[server]\nhost-name=old\ndeny-interfaces=eth1\n'
                    'use-ipv6=yes\n[publish]\npublish-workstation=yes\n'
                    '[reflector]\nenable-reflector=yes\n')
        result = config.avahi_config(original, 'evi3')
        for text in ('host-name=evi3', 'allow-interfaces=eth1', 'deny-interfaces=qca0',
                     'enable-reflector=no', 'use-ipv6=yes', 'publish-workstation=yes', '# custom'):
            self.assertIn(text, result)
        self.assertEqual(config.avahi_config(result, 'evi3'), result)
        self.assertIn('[server]', config.avahi_config('', 'evi3'))

    def test_package_states_and_no_upgrade_plan(self):
        installed = config.parse_packages('libc6\tarmhf\t2.28-10\thold ok installed\n')
        self.assertEqual(installed, {'libc6:armhf': '2.28-10'})
        for plan in ('Inst libc6 [2.28-10] (2.28-11 Debian:10 [armhf])',
                     'Conf libc6 (2.28-10 Debian:10 [armhf])', 'Remv libc6', 'Purg libc6'):
            with self.assertRaises(RuntimeError):
                config.apt_plan(plan, installed)
        plan = 'Inst avahi-daemon (0.7-4 Debian:10 [armhf])\nConf avahi-daemon (0.7-4 Debian:10 [armhf])'
        self.assertEqual(config.apt_plan(plan, installed), ['avahi-daemon'])
        with self.assertRaises(RuntimeError):
            config.parse_packages('apt\tarmhf\t1.8.2.1\tinstall ok unpacked\n')

    def test_service_policy_restored_after_failure_and_crash(self):
        for existing in (False, True):
            with self.subTest(existing=existing), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                state = root / 'state'
                state.mkdir()
                policy = root / 'policy-rc.d'
                original = b'#!/bin/sh\nexit 42\n'
                if existing:
                    policy.write_bytes(original)
                    policy.chmod(0o750)
                with patch.object(config, 'STATE', state), patch.object(config, 'POLICY', policy):
                    with self.assertRaisesRegex(RuntimeError, 'install failed'):
                        with config.block_package_services():
                            self.assertEqual(policy.read_bytes(), config.DENY)
                            raise RuntimeError('install failed')
                    self.assertEqual(policy.exists(), existing)
                    if existing:
                        self.assertEqual(policy.read_bytes(), original)
                        self.assertEqual(stat.S_IMODE(policy.stat().st_mode), 0o750)
                    self.assertFalse((state / 'policy-journal.json').exists())
                    # Model a process killed after writing its deny policy.
                    (state / 'policy-journal.json').write_text(json.dumps({'existed': existing}))
                    if existing:
                        (state / 'policy-rc.d.original').write_bytes(original)
                    policy.write_bytes(config.DENY)
                    config.restore_policy()
                    self.assertEqual(policy.exists(), existing)
                    if existing:
                        self.assertEqual(policy.read_bytes(), original)

    def test_policy_does_not_overwrite_another_admin_change(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            policy = state / 'policy'
            policy.write_bytes(b'other change')
            (state / 'policy-journal.json').write_text('{"existed": false}')
            with patch.object(config, 'STATE', state), patch.object(config, 'POLICY', policy):
                with self.assertRaises(RuntimeError):
                    config.restore_policy()
                self.assertEqual(policy.read_bytes(), b'other change')

    def test_download_requires_https_and_authenticated_digest(self):
        class Opener:
            def open(self, url, timeout):
                return io.BytesIO(b'payload')
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / 'download'
            config.download(Opener(), 'https://example.invalid/file', destination,
                            hashlib.sha256(b'payload').hexdigest())
            with self.assertRaises(ValueError):
                config.download(Opener(), 'https://example.invalid/file', destination, '0' * 64)
            with self.assertRaises(ValueError):
                config.download(Opener(), 'https://example.invalid/file', destination, limit=2)
            with self.assertRaises(ValueError):
                config.download(Opener(), 'http://example.invalid/file', destination)

    def test_signed_index_chain_and_signature_failure(self):
        payload = b'authenticated package'
        digest = hashlib.sha256(payload).hexdigest()
        index = lzma.compress(('Package: apt\nVersion: 1.8.2.1\nArchitecture: armhf\n'
            'Filename: pool/main/a/apt/apt_1.8.2.1_armhf.deb\nSHA256: ' + digest + '\n\n').encode())
        release = ('Codename: buster\nSHA256:\n {} {} main/binary-armhf/Packages.xz\n'.format(
            hashlib.sha256(index).hexdigest(), len(index))).encode()
        calls = []

        class Opener:
            def open(self, url, timeout):
                calls.append(url)
                if url.endswith('Release.gpg'):
                    return io.BytesIO(b'signature fixture')
                if url.endswith('Release'):
                    return io.BytesIO(release)
                return io.BytesIO(index if url.endswith('.xz') else payload)

        with tempfile.TemporaryDirectory() as directory:
            valid = ('[GNUPG:] GOODSIG ' + 'A' * 16 + ' trusted signer\n'
                     '[GNUPG:] VALIDSIG ' + 'A' * 40 + ' 2020-01-01 1 0 4 0 1 8 00\n')
            with patch.object(config, 'run', return_value=subprocess.CompletedProcess([], 0, valid, '')) as check:
                found = config.authenticated_apt_packages(Opener(), {'apt': '1.8.2.1'}, Path(directory))
                self.assertEqual(found['apt'].read_bytes(), payload)
                self.assertIn('gpgv', check.call_args[0][0])
            calls.clear()
            with patch.object(config, 'run', side_effect=RuntimeError('bad signature')):
                with self.assertRaisesRegex(RuntimeError, 'bad signature'):
                    config.authenticated_apt_packages(Opener(), {'apt': '1.8.2.1'}, Path(directory))
            self.assertEqual(len(calls), 2)

    def test_snapshot_signatures_require_a_valid_trusted_signature_and_only_tolerate_missing_extra_keys(self):
        valid = ('[GNUPG:] GOODSIG ' + 'A' * 16 + ' trusted signer\n'
                 '[GNUPG:] VALIDSIG ' + 'A' * 40 + ' 2023-02-28 1 0 4 0 1 8 00\n')
        missing = '[GNUPG:] ERRSIG 1234 1 8 00 1 9\n[GNUPG:] NO_PUBKEY 1234\n'
        expired = ('[GNUPG:] KEYEXPIRED 1747766350\n[GNUPG:] EXPKEYSIG ' + 'B' * 16 + ' old signer\n'
                   '[GNUPG:] VALIDSIG ' + 'B' * 40 + ' 2020-01-01 1 0 4 0 1 8 00\n')
        cases = ((0, valid, True), (2, valid + missing, True), (2, missing, False),
                 (0, expired + valid, True), (2, expired + valid + missing, True),
                 (0, expired, False), (2, expired + missing, False),
                 (0, valid.split('\n', 1)[1], False),
                 (0, valid.replace('GOODSIG ' + 'A' * 16, 'GOODSIG ' + 'C' * 16), False),
                 (0, '[GNUPG:] GOODSIG 1234 trusted name\n', False),
                 (1, valid, False), (2, valid, False),
                 (2, valid + missing.replace('1 9', '1 4'), False),
                 (2, valid + missing.replace('NO_PUBKEY 1234', 'NO_PUBKEY 5678'), False),
                 (2, valid + missing + '[GNUPG:] BADSIG 9876\n', False),
                 (2, valid + missing + '[GNUPG:] FAILURE verify 1\n', False),
                 (2, valid + missing + '[GNUPG:] NODATA 1\n', False),
                 (0, valid + '[GNUPG:] REVKEYSIG 1234\n', False))
        index = b'index fixture'
        release = 'Codename: buster\nSHA256:\n {} {} main/binary-armhf/Packages.xz\n'.format(
            hashlib.sha256(index).hexdigest(), len(index))

        def download(opener, url, path, expected=None, limit=None):
            path.write_bytes(release.encode() if url.endswith('/Release') else index)

        for code, statuses, accepted in cases:
            with self.subTest(code=code, statuses=statuses), tempfile.TemporaryDirectory() as directory:
                with patch.object(config, 'download', side_effect=download) as fetch, \
                        patch.object(config, 'run', return_value=subprocess.CompletedProcess([], code, statuses, 'signature error')):
                    if accepted:
                        self.assertEqual(config.authenticated_snapshot_index(object(), Path(directory)).read_bytes(), index)
                    else:
                        with self.assertRaisesRegex(RuntimeError, 'signature verification failed'):
                            config.authenticated_snapshot_index(object(), Path(directory))
                    self.assertEqual(fetch.call_count, 3 if accepted else 2)

    def test_restore_only_runtime_files_and_python37_syntax(self):
        for name in ('usr/bin/apt-get', 'usr/bin/apt-key', 'usr/lib/apt/methods/https',
                     'usr/lib/arm-linux-gnueabihf/libapt-pkg.so.5.0'):
            self.assertTrue(config.runtime_path(name))
        for name in ('etc/apt/sources.list', 'usr/lib/apt/apt.systemd.daily',
                     'usr/share/keyrings/debian-archive-keyring.gpg', 'etc/logrotate.d/apt'):
            self.assertFalse(config.runtime_path(name))
        config.downloader()  # Loads the pinned CA bundle, without network requests.


if __name__ == '__main__':
    unittest.main()
