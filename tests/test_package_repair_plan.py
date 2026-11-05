"""Repair planning never installs packages or changes the live database."""
import importlib.util
import json
import lzma
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('repair_configure', Path(__file__).resolve().parents[1] / 'scripts/configure.py')
config = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(config)


class PackageRepairPlanTests(unittest.TestCase):
    def test_plan_preserves_existing_packages_and_uses_private_indexes(self):
        for case in ('ok', 'healthy', 'unknown', 'missing-record', 'solver-failed', 'upgrade', 'inventory-race'):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                apt = root / 'apt'
                (apt / 'lists').mkdir(parents=True)
                (apt / 'sources.list').write_text('deb https://archive.debian.org/debian buster main\n')
                source = apt / 'apt.conf'
                source.write_text('Dir::Etc::main "/dev/null";\n')
                status = root / 'status'
                status.write_text('Package: ' + ('other' if case == 'missing-record' else 'npm') +
                                  '\nStatus: install ok installed\nVersion: 1\nArchitecture: all\nDepends: node-one | node-two\n\n')
                original = {'npm:all': '1', 'libssl1.1:armhf': '1.1.1d-0+deb10u3',
                            'zlib1g:armhf': '1:1.2.11.dfsg-1', 'avahi-daemon:armhf': '0.7-4+deb10u3'}
                calls = []

                def filesystem(value):
                    return status if str(value) == '/var/lib/dpkg/status' else Path(value)

                def download(opener, directory):
                    (directory / 'Release').write_text('Codename: buster\n')
                    (directory / 'Release.gpg').write_bytes(b'signed fixture')
                    path = directory / 'Packages.xz'
                    path.write_bytes(lzma.compress(b''))
                    return path

                def command(arguments, check=True, env=None):
                    calls.append(arguments)
                    self.assertEqual(env['LC_ALL'], 'C')
                    if arguments == ['/usr/bin/apt-get', 'check']:
                        report = ('The following packages have unmet dependencies:\n'
                                  ' npm : Depends: node-one but it is not installed\n')
                        if case == 'unknown':
                            report = report.replace(' npm :', ' unreviewed :')
                        return subprocess.CompletedProcess(arguments, 0 if case == 'healthy' else 1, report, '')
                    private = Path(env['APT_CONFIG'])
                    self.assertNotEqual(private, source)
                    self.assertIn('Dir::Cache::pkgcache "";', private.read_text())
                    self.assertIn(config.SNAPSHOT, (private.parent / 'sources.list').read_text())
                    preferences = (private.parent / 'preferences').read_text()
                    for name, version in original.items():
                        self.assertIn('Package: {}\nPin: version {}\nPin-Priority: 1001'.format(name, version), preferences)
                    if arguments[1] == 'indextargets':
                        path = next((private.parent / 'lists').glob('*_Packages'))
                        return subprocess.CompletedProcess(arguments, 0, 'Packages ' + str(path) + '\n', '')
                    self.assertEqual(arguments, ['/usr/bin/apt-get', '--simulate', '--no-remove',
                                                  '--no-upgrade', '--no-install-recommends', '--fix-broken', 'install'])
                    self.assertIn('Package: libuv1-dev\nPin: version 2\nPin-Priority: -1', preferences)
                    if case == 'inventory-race':
                        status.write_text('changed externally')
                    output = ('Inst npm [1] (2 Debian [all])\n' if case == 'upgrade' else
                              'Inst node-one (1 Debian [all])\nConf node-one (1 Debian [all])\n')
                    return subprocess.CompletedProcess(arguments, int(case == 'solver-failed'), output, '')

                before = status.read_bytes()
                with patch.object(config, 'STATE', root), patch.object(config, 'APT_STATE', apt), \
                        patch.object(config, 'Path', filesystem), patch.object(config, 'inventory', return_value=original), \
                        patch.object(config, 'run', command), patch.object(config, 'apt_ready', return_value=True), \
                        patch.object(config, 'downloader', return_value=object()), \
                        patch.object(config, 'authenticated_snapshot_index', side_effect=download) as fetch, \
                        patch.object(config, 'index_version_blocks', return_value={('libuv1-dev', '2')}):
                    if case in ('ok', 'healthy'):
                        config.plan_package_repair()
                    else:
                        with self.assertRaises(RuntimeError):
                            config.plan_package_repair()
                    self.assertEqual(fetch.call_count, int(case not in ('healthy', 'unknown', 'missing-record')))
                self.assertEqual(source.read_text(), 'Dir::Etc::main "/dev/null";\n')
                self.assertEqual(list((apt / 'lists').iterdir()), [])
                if case != 'inventory-race':
                    self.assertEqual(status.read_bytes(), before)
                self.assertTrue(all(args[0] == '/usr/bin/apt-get' and
                                    ('--simulate' in args or args[1] in ('check', 'indextargets')) for args in calls))

    @unittest.skipUnless(Path('/usr/lib/apt/apt-helper').is_file() and shutil.which('apt-get'), 'APT is required')
    def test_version_blocks_preserve_alternatives_and_installed_packages(self):
        records = [
            {'Package': 'new-dev', 'Version': '2', 'Architecture': 'armhf', 'Depends': 'runtime:any (= 2)'},
            {'Package': 'new-dev', 'Version': '1', 'Architecture': 'armhf', 'Depends': 'runtime (= 1)'},
            {'Package': 'pre-dev', 'Version': '2', 'Architecture': 'all', 'Pre-Depends': 'runtime (= 2)'},
            {'Package': 'alternative', 'Version': '2', 'Architecture': 'armhf', 'Depends': 'runtime (= 2) | other'},
            {'Package': 'installed', 'Version': '2', 'Architecture': 'armhf', 'Depends': 'runtime (= 2)'},
            {'Package': 'foreign', 'Version': '2', 'Architecture': 'arm64', 'Depends': 'runtime (= 2)'},
        ]
        with tempfile.TemporaryDirectory() as temporary:
            index = Path(temporary) / 'Packages.xz'
            index.write_bytes(lzma.compress(config.control_text(records).encode()))
            self.assertEqual(config.index_version_blocks(index, {'runtime:armhf': '1', 'installed:armhf': '1'}),
                             {('new-dev', '2'), ('pre-dev', '2')})

    @unittest.skipUnless(Path('/usr/lib/apt/apt-helper').is_file() and shutil.which('apt-get'), 'APT is required')
    def test_real_apt_repairs_transitive_dependencies_with_matching_library_versions(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            apt = root / 'apt'
            lists = apt / 'lists'
            lists.mkdir(parents=True)
            empty = apt / 'empty'
            empty.mkdir()
            libraries = ('libssl1.1', 'zlib1g', 'libbz2-1.0', 'libuv1')
            devs = ('libssl-dev', 'zlib1g-dev', 'libbz2-dev', 'libuv1-dev')

            def record(name, version='1', depends='', installed=False):
                result = {'Package': name, 'Version': version, 'Architecture': 'armhf',
                          'Maintainer': 'Test <test@example.invalid>', 'Description': name}
                if depends:
                    result['Depends'] = depends
                if installed:
                    result['Status'] = 'install ok installed'
                else:
                    result.update(Filename='pool/' + name + '.deb', Size='1')
                return result

            originals = [record(name, installed=True) for name in libraries]
            originals.append(record('npm', depends=', '.join(devs) + ', node-one', installed=True))
            status = root / 'status'
            before = config.control_text(originals)
            status.write_text(before)
            original = {r['Package'] + ':armhf': r['Version'] for r in originals}
            archive = [record(dev, '2', lib + ' (= 2)') for dev, lib in zip(devs, libraries)]
            # This compatible version is in the archive, while the older snapshot has libuv 0.
            archive.append(record('libuv1-dev', '1', 'libuv1 (= 1)'))
            archive += [record('node-one', depends='node-two'), record('node-two')]
            (lists / 'archive.debian.org_debian_dists_buster_main_binary-armhf_Packages').write_text(config.control_text(archive))
            (apt / 'sources.list').write_text('deb [arch=armhf] https://archive.debian.org/debian buster main\n')
            options = {'Dir::Etc::main': '/dev/null', 'Dir::Etc::parts': str(empty),
                       'Dir::Etc::sourcelist': str(apt / 'sources.list'), 'Dir::Etc::sourceparts': str(empty),
                       'Dir::Etc::preferences': '/dev/null', 'Dir::Etc::preferencesparts': str(empty),
                       'Dir::State::status': str(status), 'Dir::State::lists': str(lists),
                       'Dir::Cache::pkgcache': '', 'Dir::Cache::srcpkgcache': '', 'APT::Architecture': 'armhf'}
            (apt / 'apt.conf').write_text(''.join('{} {};\n'.format(k, json.dumps(v)) for k, v in options.items()))

            def snapshot(opener, directory):
                records = [record(dev, '0' if lib == 'libuv1' else '1',
                                  lib + (' (= 0)' if lib == 'libuv1' else ' (= 1)'))
                           for dev, lib in zip(devs, libraries)]
                (directory / 'Release').write_text('Codename: buster\n')
                (directory / 'Release.gpg').write_bytes(b'fixture')
                index = directory / 'Packages.xz'
                index.write_bytes(lzma.compress(config.control_text(records).encode()))
                return index

            def filesystem(value):
                return status if str(value) == '/var/lib/dpkg/status' else Path(value)

            with patch.object(config, 'STATE', root), patch.object(config, 'APT_STATE', apt), \
                    patch.object(config, 'Path', filesystem), patch.object(config, 'inventory', return_value=original), \
                    patch.object(config, 'apt_ready', return_value=True), patch.object(config, 'downloader'), \
                    patch.object(config, 'authenticated_snapshot_index', side_effect=snapshot):
                config.plan_package_repair()
            plan = next(root.glob('package-repair-plan-*/plan.txt')).read_text()
            names = config.apt_plan(plan, original)
            self.assertEqual(set(names), set(devs) | {'node-one', 'node-two'})
            for dev in devs:
                self.assertIn('Inst ' + dev + ' (1 ', plan)
            self.assertEqual(status.read_text(), before)


if __name__ == '__main__':
    unittest.main()
