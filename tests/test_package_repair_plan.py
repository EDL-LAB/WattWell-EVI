"""Repair planning never installs packages or changes the live database."""
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('repair_configure', Path(__file__).resolve().parents[1] / 'scripts/configure.py')
config = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(config)


class PackageRepairPlanTests(unittest.TestCase):
    def test_plan_uses_matching_authenticated_debs_and_preserves_existing_packages(self):
        for case in ('ok', 'healthy', 'unknown', 'alternative', 'solver-failed', 'upgrade', 'inventory-race'):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                apt = root / 'apt'
                apt.mkdir()
                source = apt / 'apt.conf'
                source.write_text('Dir::Etc::main "/dev/null";\n')
                status = root / 'status'
                dependency = 'node-one | node-two' if case == 'alternative' else 'node-one (>= 1)'
                status.write_text('Package: npm\nStatus: install ok installed\nVersion: 1\nArchitecture: all\n'
                                  'Depends: libssl-dev, zlib1g-dev, ' + dependency + '\n\n')
                original = {'npm:all': '1', 'libssl1.1:armhf': '1.1.1d-0+deb10u3',
                            'zlib1g:armhf': '1:1.2.11.dfsg-1', 'avahi-daemon:armhf': '0.7-4+deb10u3'}
                calls = []

                def filesystem(value):
                    return status if str(value) == '/var/lib/dpkg/status' else Path(value)

                def download(opener, versions, directory):
                    self.assertEqual(versions, {'libssl-dev': '1.1.1d-0+deb10u3',
                                                'zlib1g-dev': '1:1.2.11.dfsg-1'})
                    result = {name: directory / (name + '.deb') for name in versions}
                    for path in result.values():
                        path.write_bytes(b'authenticated fixture')
                    return result

                def command(arguments, check=True, env=None):
                    calls.append(arguments)
                    self.assertEqual(env['LC_ALL'], 'C')
                    if arguments == ['/usr/bin/apt-get', 'check']:
                        report = ('The following packages have unmet dependencies:\n'
                                  ' npm : Depends: node-one but it is not installed\n')
                        if case == 'unknown':
                            report = report.replace(' npm :', ' unreviewed :')
                        return subprocess.CompletedProcess(arguments, 0 if case == 'healthy' else 1, report, '')
                    self.assertEqual(arguments[:8], ['/usr/bin/apt-get', '--simulate', '--no-remove',
                                                     '--no-upgrade', '--no-install-recommends',
                                                     '--fix-broken', 'install', 'node-one'])
                    self.assertEqual([Path(arg).name for arg in arguments[8:]], ['libssl-dev.deb', 'zlib1g-dev.deb'])
                    private = Path(env['APT_CONFIG'])
                    self.assertNotEqual(private, source)
                    self.assertIn('Dir::Cache::pkgcache "";', private.read_text())
                    preferences = (private.parent / 'preferences').read_text()
                    for name, version in original.items():
                        self.assertIn('Package: {}\nPin: version {}\nPin-Priority: 1001'.format(name, version), preferences)
                    if case == 'inventory-race':
                        status.write_text('changed externally')
                    output = ('Inst npm [1] (2 Debian [all])\n' if case == 'upgrade' else
                              'Inst node-one (1 Debian [all])\nConf node-one (1 Debian [all])\n')
                    return subprocess.CompletedProcess(arguments, int(case == 'solver-failed'), output, 'solver error' if case == 'solver-failed' else '')

                before = status.read_bytes()
                with patch.object(config, 'STATE', root), patch.object(config, 'APT_STATE', apt), \
                        patch.object(config, 'Path', filesystem), patch.object(config, 'inventory', return_value=original), \
                        patch.object(config, 'run', command), patch.object(config, 'apt_ready', return_value=True), \
                        patch.object(config, 'downloader', return_value=object()), \
                        patch.object(config, 'authenticated_apt_packages', side_effect=download) as fetch:
                    if case in ('ok', 'healthy'):
                        config.plan_package_repair()
                    else:
                        with self.assertRaises(RuntimeError):
                            config.plan_package_repair()
                    self.assertEqual(fetch.call_count, int(case not in ('healthy', 'unknown', 'alternative')))
                self.assertEqual(source.read_text(), 'Dir::Etc::main "/dev/null";\n')
                if case != 'inventory-race':
                    self.assertEqual(status.read_bytes(), before)
                self.assertTrue(all(args[0] == '/usr/bin/apt-get' and
                                    ('--simulate' in args or args[1:] == ['check']) for args in calls))


if __name__ == '__main__':
    unittest.main()
