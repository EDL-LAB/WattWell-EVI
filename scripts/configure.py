#!/usr/bin/python3
"""One entry point for the observed Watt&Well Buster armhf image; Python 3.7+."""
import contextlib
import fcntl
import hashlib
import ipaddress
import json
import lzma
import os
from pathlib import Path, PurePosixPath
import pwd
import re
import shutil
import signal
import ssl
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

PROJECT = Path(__file__).resolve().parents[1]
STATE = Path('/var/lib/wattwell-evi')
APT_STATE = Path('/var/lib/wattwell-evi-apt')
CA_SHA256 = '796f1389bfea13c0ed78f663b765baaff54a508e5483786bd6ece73c5e3e3cbe'
SNAPSHOT = 'https://snapshot.debian.org/archive/debian/20200901T000000Z'
ARCHIVE = 'https://archive.debian.org/debian'
KEYRING = Path('/usr/share/keyrings/debian-archive-keyring.gpg')
POLICY = Path('/usr/sbin/policy-rc.d')
DENY = b'#!/bin/sh\n# wattwell-evi: temporary package service guard\nexit 101\n'
# Exact postinst reviewed on ww-distro-dh-v1.3.27, ignoring surrounding whitespace.
LIBC_BIN_POSTINST_SHA256 = 'f123072522ffd0657396fd6aeed799f8dcd1cfb6863550bbcba95b7da74ee17b'
PKG_CONFIG_ARCH_HOOK = ('post-invoke=if { test "$DPKG_HOOK_ACTION" = add-architecture || '
                        'test "$DPKG_HOOK_ACTION" = remove-architecture; } && '
                        'test -x /usr/share/pkg-config-dpkghook; then '
                        '/usr/share/pkg-config-dpkghook update; fi')


def run(args, check=True, env=None):
    result = subprocess.run([str(arg) for arg in args], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, universal_newlines=True, env=env)
    if check and result.returncode:
        raise RuntimeError('{}: {}'.format(args[0], (result.stdout + result.stderr)[-6000:]))
    return result


def valid_hostname(name):
    return bool(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', name))


def hosts_with_name(content, name):
    if not valid_hostname(name):
        raise ValueError('Invalid hostname.')
    lines = content.splitlines(keepends=True)
    matches = [i for i, line in enumerate(lines)
               if line.split('#', 1)[0].split()[:1] == ['127.0.1.1']]
    if len(matches) > 1:
        raise ValueError('Multiple 127.0.1.1 entries found; cannot safely choose one to change.')
    if not matches:
        return content + ('' if not content or content.endswith('\n') else '\n') + '127.0.1.1 {}\n'.format(name)
    index = matches[0]
    body, marker, comment = lines[index].partition('#')
    names = list(dict.fromkeys([name] + body.split()[1:]))
    replacement = '127.0.1.1 ' + ' '.join(names)
    if marker:
        replacement += ' #' + comment.rstrip('\r\n')
    lines[index] = replacement + '\n'
    return ''.join(lines)


def replace_file(path, data, backup=True, mode=0o644):
    """Keep one original backup and atomically replace regular configuration files."""
    path = Path(path)
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise RuntimeError('Destination is not a regular file: {}'.format(path))
    before = path.read_bytes() if path.exists() else None
    if before == data:
        return False
    info = path.stat() if path.exists() else None
    if backup and info:
        saved = STATE / 'backups' / str(path).lstrip('/')
        saved.parent.mkdir(parents=True, exist_ok=True)
        if not saved.exists():
            shutil.copy2(str(path), str(saved))
            os.chown(str(saved), info.st_uid, info.st_gid)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'wb') as output:
            output.write(data)
            output.flush()
            os.fchmod(output.fileno(), stat.S_IMODE(info.st_mode) if info else mode)
            if info:
                os.fchown(output.fileno(), info.st_uid, info.st_gid)
            os.fsync(output.fileno())
        if info:
            shutil.copystat(str(path), temporary)
        os.replace(temporary, str(path))
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return True


def configure_name(name):
    hosts = Path('/etc/hosts')
    hostname_file = Path('/etc/hostname')
    previous_name = run(['hostname']).stdout.strip()
    previous = {path: path.read_bytes() if path.exists() else None
                for path in (hosts, hostname_file)}
    try:
        replace_file(hosts, hosts_with_name(hosts.read_text(), name).encode())
        replace_file(hostname_file, (name + '\n').encode())
        if previous_name != name:
            run(['hostname', name])
        run(['getent', 'hosts', name])
    except BaseException:
        for path, content in previous.items():
            if content is not None:
                replace_file(path, content, backup=False)
        if run(['hostname']).stdout.strip() != previous_name:
            run(['hostname', previous_name])
        raise


def parse_packages(text):
    packages = {}
    for line in text.splitlines():
        name, architecture, version, status = line.split('\t')
        if status in ('deinstall ok config-files', 'purge ok config-files'):
            continue
        if status not in ('install ok installed', 'hold ok installed'):
            raise RuntimeError('Package is in an incomplete state: {} {}'.format(name, status))
        packages[name + ':' + architecture] = version
    return packages


def inventory(repair_libc=False):
    text = run(['dpkg-query', '-W',
        '-f=${Package}\t${Architecture}\t${Version}\t${Status}\n']).stdout
    try:
        known = 'libc-bin\tarmhf\t2.28-10\tinstall ok half-configured'
        if repair_libc and known in text.splitlines():
            expected = parse_packages(text.replace(known, known.replace('half-configured', 'installed')))
            configure_libc_bin()
            after = inventory()
            if after != expected:
                raise RuntimeError('The inventory changed during libc-bin configuration; stop for review.')
            return after
        return parse_packages(text)
    except RuntimeError as error:
        try:
            report = package_failure_report(text)
        except Exception as diagnostic_error:
            raise RuntimeError('{}\nDiagnostic collection also failed: {}'.format(error, diagnostic_error)) from error
        raise RuntimeError('{}\nRead-only diagnostics saved to {}.\n'
                           'Share the contents of that file to identify the cause; '
                           'do not run dpkg --configure -a or apt-get -f install.'.format(error, report)) from error


def package_failure_report(text):
    sections = ['This diagnostic collection is read-only; it does not execute maintainer scripts.\n']
    scripts = []
    for line in text.splitlines():
        name, architecture, version, status = line.split('\t')
        if status in ('install ok installed', 'hold ok installed',
                      'deinstall ok config-files', 'purge ok config-files'):
            continue
        sections.append(line + '\n')
        if not re.fullmatch(r'[a-z0-9][a-z0-9+.-]*', name) or not re.fullmatch(r'[a-z0-9-]+', architecture):
            raise ValueError('Invalid package identity in diagnostics.')
        scripts.extend('/var/lib/dpkg/info/{}.postinst'.format(package)
                       for package in (name, name + ':' + architecture))
    audit = run(['dpkg', '--audit'], check=False)
    sections.append('\n$ dpkg --audit (exit {})\n{}{}'.format(audit.returncode, audit.stdout, audit.stderr))
    for filename in scripts + ['/var/log/apt/term.log', '/var/log/dpkg.log']:
        path = Path(filename)
        sections.append('\n--- {} ---\n'.format(filename))
        try:
            with path.open('rb') as source:
                size = source.seek(0, os.SEEK_END)
                start = max(0, size - 16384) if filename.endswith('.log') else 0
                source.seek(start)
                data = source.read(16384)
            if start or size > len(data):
                sections.append('[excerpt limited to 16384 bytes]\n')
            sections.append(data.decode('utf-8', errors='replace') + '\n')
        except OSError as error:
            sections.append('Unavailable: {}\n'.format(error))
    for filename in ('/sbin/ldconfig', '/usr/sbin/ldconfig'):
        sections.append('{}: exists={}, executable={}\n'.format(
            filename, Path(filename).exists(), os.access(filename, os.X_OK)))
    report = STATE / 'package-state-report.txt'
    replace_file(report, ''.join(sections).encode(), backup=False, mode=0o600)
    return report


def static_addresses(text):
    addresses = []
    for line in text.splitlines():
        if not line.strip():
            continue
        fields = line.replace('\\', ' ').split()
        if len(fields) < 4 or not re.fullmatch(r'[0-9]+:', fields[0]):
            raise ValueError('Unexpected format in the one-line IPv4 inventory.')
        if fields[1:3] != ['eth1', 'inet']:
            raise ValueError('Unexpected interface in the inventory.')
        if fields.count('scope') != 1 or fields.count('valid_lft') != 1:
            raise ValueError('Missing or ambiguous scope/valid_lft in the IPv4 inventory.')
        scope = fields.index('scope')
        lifetime = fields.index('valid_lft')
        if scope + 1 >= len(fields) or lifetime + 1 >= len(fields) or lifetime <= scope + 1:
            raise ValueError('Incomplete IPv4 inventory.')
        if not re.fullmatch(r'forever|[0-9]+sec', fields[lifetime + 1]):
            raise ValueError('Unexpected IPv4 lifetime in the inventory.')
        if fields[scope + 1] != 'global' or fields[lifetime + 1] != 'forever' or 'dynamic' in fields:
            continue
        if '/' not in fields[3]:
            raise ValueError('Missing IPv4 address prefix.')
        address = str(ipaddress.IPv4Interface(fields[3]))
        label = fields[lifetime - 1]
        if not re.fullmatch(r'eth1(?::[a-zA-Z0-9_-]{1,10})?', label):
            raise ValueError('Unexpected IPv4 label: {}'.format(label))
        addresses.append((address, label))
    return sorted(set(addresses))


def configure_dhcp(name):
    """Keep current leases; add a separate guarded client for subsequent boots."""
    unit = Path('/etc/systemd/system/wattwell-eth1-dhcp.service')
    if unit.exists() and not (STATE / 'static-addresses').is_file():
        raise RuntimeError('The saved IPv4 inventory for the existing DHCP service is missing.')
    if not unit.exists():
        addresses = static_addresses(run(['ip', '-4', '-o', 'addr', 'show', 'dev', 'eth1']).stdout)
        if not addresses:
            raise RuntimeError('No permanent IPv4 addresses found on eth1; unknown configuration.')
        replace_file(STATE / 'static-addresses',
                     ''.join('{} {}\n'.format(*pair) for pair in addresses).encode())
    helper = Path('/usr/local/lib/wattwell-evi')
    for filename in ('start-dhcp.sh', 'dhclient-hook.sh'):
        replace_file(helper / filename, (PROJECT / 'scripts' / filename).read_bytes(), mode=0o755)
    client_config = ('timeout 60;\nretry 60;\n'
                     'request subnet-mask, broadcast-address, routers, domain-name-servers, domain-search;\n'
                     'supersede host-name "{}";\n'.format(name))
    replace_file(STATE / 'dhclient.conf', client_config.encode())
    service = ('[Unit]\nDescription=WattWell EVI DHCPv4 on eth1\n'
               'BindsTo=sys-subsystem-net-devices-eth1.device\n'
               'After=network.target systemd-networkd.service sys-subsystem-net-devices-eth1.device\n\n'
               '[Service]\nType=simple\n'
               'ExecStart=/bin/sh /usr/local/lib/wattwell-evi/start-dhcp.sh\n'
               'KillMode=control-group\n\n[Install]\nWantedBy=multi-user.target\n')
    changed = replace_file(unit, service.encode())
    if changed:
        run(['systemctl', 'daemon-reload'])
    run(['systemctl', 'enable', 'wattwell-eth1-dhcp.service'])
    # start-dhcp refuses another client; enabling does not restart an existing lease.
    run(['sh', str(helper / 'start-dhcp.sh'), '--once'])
    if run(['hostname']).stdout.strip() != name:
        run(['hostname', name])


class HTTPSRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        if urllib.parse.urlsplit(new_url).scheme != 'https':
            raise RuntimeError('Redirect to unauthenticated transport refused.')
        return super().redirect_request(request, fp, code, message, headers, new_url)


def downloader():
    bundle = PROJECT / 'certs' / 'cacert.pem'
    if hashlib.sha256(bundle.read_bytes()).hexdigest() != CA_SHA256:
        raise RuntimeError('The project certificate bundle does not match the expected hash.')
    context = ssl.create_default_context(cafile=str(bundle))
    return urllib.request.build_opener(HTTPSRedirect(), urllib.request.HTTPSHandler(context=context))


def download(opener, url, path, expected=None, limit=100 * 1024 * 1024):
    if urllib.parse.urlsplit(url).scheme != 'https':
        raise ValueError('Only HTTPS is allowed.')
    path = Path(path)
    digest = hashlib.sha256()
    size = 0
    for attempt in range(3):
        try:
            source = opener.open(url, timeout=30)
            break
        except (urllib.error.URLError, TimeoutError):
            if attempt == 2:
                raise
            time.sleep(attempt + 1)
    with source, path.open('wb') as target:
        while True:
            chunk = source.read(65536)
            if not chunk:
                break
            size += len(chunk)
            if size > limit:
                raise ValueError('Download exceeds the expected size limit.')
            digest.update(chunk)
            target.write(chunk)
    if expected and digest.hexdigest() != expected:
        raise ValueError('SHA-256 mismatch: {}'.format(url))


def paragraphs(lines):
    fields = {}
    key = None
    for raw in lines:
        line = raw.rstrip('\r\n')
        if not line:
            if fields:
                yield fields
            fields, key = {}, None
        elif line[:1].isspace() and key:
            fields[key] += '\n' + line[1:]
        else:
            key, separator, value = line.partition(':')
            if not separator or key in fields:
                raise ValueError('Malformed Debian metadata.')
            fields[key] = value.strip()
    if fields:
        yield fields


def authenticated_apt_packages(opener, versions, directory):
    print('Fetching the exact APT version from an authenticated Debian snapshot...', flush=True)
    release = directory / 'Release'
    signature = directory / 'Release.gpg'
    download(opener, SNAPSHOT + '/dists/buster/Release', release, limit=1024 * 1024)
    download(opener, SNAPSHOT + '/dists/buster/Release.gpg', signature, limit=1024 * 1024)
    run(['gpgv', '--homedir', str(directory), '--keyring', str(KEYRING), str(signature), str(release)])
    metadata = next(paragraphs(release.read_text().splitlines()))
    if metadata.get('Codename') != 'buster':
        raise ValueError('The snapshot is not Buster.')
    index_name = 'main/binary-armhf/Packages.xz'
    checksum = None
    for line in metadata['SHA256'].splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[2] == index_name:
            checksum = parts[0]
    if not checksum or not re.fullmatch('[0-9a-f]{64}', checksum):
        raise ValueError('The authenticated armhf index hash is missing.')
    index = directory / 'Packages.xz'
    download(opener, SNAPSHOT + '/dists/buster/' + index_name, index, checksum)
    wanted = dict(versions)
    found = {}
    with lzma.open(str(index), 'rt') as data:
        for entry in paragraphs(data):
            name = entry.get('Package')
            if (name not in wanted or entry.get('Version') != wanted[name]
                    or entry.get('Architecture') != 'armhf'):
                continue
            filename = PurePosixPath(entry['Filename'])
            if filename.is_absolute() or '..' in filename.parts or not str(filename).startswith('pool/'):
                raise ValueError('Unexpected package path.')
            sha = entry['SHA256']
            if not re.fullmatch('[0-9a-f]{64}', sha):
                raise ValueError('Invalid package hash.')
            path = directory / (name + '.deb')
            download(opener, SNAPSHOT + '/' + str(filename), path, sha)
            found[name] = path
    if set(found) != set(wanted):
        raise RuntimeError('The installed version is not in the supported snapshot; no upgrade was performed.')
    return found


def runtime_path(name):
    return (name in ('usr/bin/apt', 'usr/bin/apt-get', 'usr/bin/apt-cache',
                     'usr/bin/apt-config', 'usr/bin/apt-mark', 'usr/lib/apt/apt-helper')
            or name.startswith('usr/lib/apt/methods/')
            or name.startswith('usr/lib/arm-linux-gnueabihf/libapt-pkg.so.'))


def apt_ready():
    required = ['/usr/bin/apt-get', '/usr/bin/apt-config', '/usr/bin/apt-cache',
                '/usr/lib/apt/methods/http', '/usr/lib/apt/methods/https', '/usr/lib/apt/methods/gpgv']
    return (all(Path(path).is_file() for path in required)
            and run(['/usr/bin/apt-get', '--version'], check=False).returncode == 0)


def restore_apt(opener, packages):
    if apt_ready():
        return
    versions = {name: packages.get(name + ':armhf') for name in ('apt', 'libapt-pkg5.0')}
    if set(versions.values()) != {'1.8.2.1'}:
        raise RuntimeError('APT recovery is limited to registered version 1.8.2.1; no automatic upgrade.')
    with tempfile.TemporaryDirectory(prefix='restore-apt-', dir=str(STATE)) as temporary:
        directory = Path(temporary)
        debs = authenticated_apt_packages(opener, versions, directory)
        for name, package in debs.items():
            fields = run(['dpkg-deb', '-f', str(package), 'Package', 'Version', 'Architecture']).stdout
            if ('Package: ' + name not in fields or 'Version: ' + versions[name] not in fields
                    or 'Architecture: armhf' not in fields):
                raise ValueError('Package identity does not match the metadata.')
            extracted = directory / name
            run(['dpkg-deb', '-x', str(package), str(extracted)])
            for source in sorted(extracted.rglob('*')):
                relative = source.relative_to(extracted).as_posix()
                if not runtime_path(relative) or source.is_dir():
                    continue
                target = Path('/') / relative
                if target.exists() or target.is_symlink():
                    continue
                if not str(target.parent.resolve()).startswith('/usr/'):
                    raise ValueError('Recovery destination is outside /usr.')
                missing = []
                parent = target.parent
                while not parent.exists():
                    missing.append(parent)
                    parent = parent.parent
                for parent in reversed(missing):
                    parent.mkdir(mode=0o755)
                    parent.chmod(0o755)
                if source.is_symlink():
                    link = os.readlink(str(source))
                    if not str((target.parent / link).resolve()).startswith('/usr/'):
                        raise ValueError('Package symlink points outside /usr.')
                    target.symlink_to(link)
                else:
                    replace_file(target, source.read_bytes(), mode=stat.S_IMODE(source.stat().st_mode))
    # Never run dpkg maintainer scripts, restore vendor configs, or fabricate a .list.
    run(['/usr/bin/apt-get', '--version'])


def avahi_config(content, name):
    desired = {'server': {'host-name': name, 'allow-interfaces': 'eth1', 'deny-interfaces': 'qca0'},
               'reflector': {'enable-reflector': 'no'}}
    seen = {section: set() for section in desired}
    section = None
    output = []

    def missing(current):
        if current in desired:
            for key, value in desired[current].items():
                if key not in seen[current]:
                    output.append('{}={}\n'.format(key, value))
                    seen[current].add(key)

    for line in content.splitlines(keepends=True):
        header = re.fullmatch(r'\s*\[([^]]+)\]\s*', line.strip())
        if header:
            missing(section)
            section = header.group(1).lower()
        assignment = re.match(r'\s*([^#;=\s]+)\s*=', line)
        key = assignment.group(1) if assignment else None
        if section in desired and key in desired[section]:
            if key not in seen[section]:
                output.append('{}={}\n'.format(key, desired[section][key]))
                seen[section].add(key)
        else:
            output.append(line if line.endswith('\n') else line + '\n')
    missing(section)
    for section in desired:
        if not seen[section]:
            output.append('\n[{}]\n'.format(section))
            missing(section)
    return ''.join(output)


def apt_plan(output, installed):
    installed_names = {key.split(':', 1)[0] for key in installed}
    new = []
    for line in output.splitlines():
        if line.startswith(('Remv ', 'Purg ')):
            raise RuntimeError('The APT plan removes packages; operation refused.')
        if line.startswith(('Inst ', 'Conf ')):
            fields = line.split()
            name = fields[1].split(':', 1)[0]
            if name in installed_names:
                raise RuntimeError('The plan changes an existing package: ' + name)
            if fields[0] == 'Inst':
                new.append(name)
    return new


def restore_policy():
    journal = STATE / 'policy-journal.json'
    if not journal.exists():
        return
    if POLICY.is_symlink():
        raise RuntimeError('The service policy was replaced with a symlink; preserve the journal.')
    record = json.loads(journal.read_text())
    original = STATE / 'policy-rc.d.original'
    current = POLICY.read_bytes() if POLICY.exists() else None
    old = original.read_bytes() if record['existed'] else None
    if current not in (DENY, old):
        raise RuntimeError('The service policy changed; preserve and review ' + str(journal))
    if current == DENY:
        if record['existed']:
            os.replace(str(original), str(POLICY))
        else:
            POLICY.unlink()
    elif original.exists():
        original.unlink()
    journal.unlink()


@contextlib.contextmanager
def block_package_services():
    if POLICY.is_symlink() or (POLICY.exists() and not POLICY.is_file()):
        raise RuntimeError('policy-rc.d is not a regular file; it will not be replaced.')
    original = STATE / 'policy-rc.d.original'
    existed = POLICY.exists()
    if existed:
        shutil.copy2(str(POLICY), str(original))
        info = POLICY.stat()
        os.chown(str(original), info.st_uid, info.st_gid)
    replace_file(STATE / 'policy-journal.json', json.dumps({'existed': existed}).encode(), backup=False)
    try:
        replace_file(POLICY, DENY, backup=False, mode=0o755)
        POLICY.chmod(0o755)
        yield
    finally:
        restore_policy()


def configure_libc_bin():
    postinst = Path('/var/lib/dpkg/info/libc-bin.postinst')
    if postinst.is_symlink() or hashlib.sha256(postinst.read_bytes().strip()).hexdigest() != LIBC_BIN_POSTINST_SHA256:
        raise RuntimeError('The libc-bin postinst differs from the reviewed script; it will not be executed.')
    if run(['dpkg-query', '-W', '-f=${Config-Version}', 'libc-bin']).stdout.strip() != '2.28-10':
        raise RuntimeError('libc-bin Config-Version does not match 2.28-10; stop for review.')
    nsswitch = Path('/etc/nsswitch.conf')
    default = Path('/usr/share/libc-bin/nsswitch.conf')
    if nsswitch.is_symlink() or default.is_symlink() or nsswitch.read_bytes() != default.read_bytes():
        raise RuntimeError('nsswitch.conf differs from the existing template; preserve the configuration and stop.')
    configs = [Path('/etc/dpkg/dpkg.cfg'), Path(os.path.expanduser('~/.dpkg.cfg'))]
    configs.extend(Path('/etc/dpkg/dpkg.cfg.d').glob('*'))
    for path in configs:
        if not path.is_file():
            continue
        content = path.read_text()
        # This exact reviewed hook does nothing for DPKG_HOOK_ACTION=configure.
        if path == Path('/etc/dpkg/dpkg.cfg.d/pkg-config-hook-config') and content.strip() == PKG_CONFIG_ARCH_HOOK:
            continue
        if re.search(r'^\s*(?:--)?(?:pre|post)-invoke\b', content, re.M):
            raise RuntimeError('An existing dpkg hook requires review: ' + str(path))
    # Both flags are required to inspect libraries without changing cache or links.
    run(['/sbin/ldconfig', '-N', '-X'])
    for filename in ('/var/lib/dpkg/status', '/etc/nsswitch.conf',
                     '/etc/ld.so.cache', '/var/cache/ldconfig/aux-cache'):
        path = Path(filename)
        if path.is_symlink():
            raise RuntimeError('Unexpected backup destination: ' + filename)
        if path.is_file():
            saved = STATE / 'backups' / filename.lstrip('/')
            saved.parent.mkdir(parents=True, exist_ok=True)
            if not saved.exists():
                shutil.copy2(str(path), str(saved))
    print('Completing only libc-bin 2.28-10, without upgrades or triggers for other packages...', flush=True)
    env = dict(os.environ, PATH='/usr/sbin:/usr/bin:/sbin:/bin', DEBIAN_FRONTEND='noninteractive')
    for key in ('DPKG_FORCE', 'DPKG_FRONTEND_LOCKED'):
        env.pop(key, None)
    with block_package_services():
        result = run(['dpkg', '--no-triggers', '--log=' + str(STATE / 'libc-bin-dpkg.log'),
                      '--configure', 'libc-bin'], check=False, env=env)
    log = STATE / 'libc-bin-configure.log'
    replace_file(log, (result.stdout + result.stderr).encode(), backup=False, mode=0o600)
    if result.returncode:
        raise RuntimeError('Targeted libc-bin configuration failed (exit {}): {}\n{}'.format(
            result.returncode, log, (result.stdout + result.stderr)[-6000:]))
    if nsswitch.read_bytes() != default.read_bytes():
        raise RuntimeError('nsswitch.conf changed during libc-bin configuration; stop for review.')


def install_avahi(original, name):
    if 'avahi-daemon:armhf' not in original:
        print('Preparing Avahi installation without upgrades...', flush=True)
        if APT_STATE.is_symlink():
            raise RuntimeError('The APT state directory cannot be a symlink.')
        APT_STATE.mkdir(mode=0o755, parents=True, exist_ok=True)
        APT_STATE.chmod(0o755)
        try:
            sandbox = pwd.getpwnam('_apt')
        except KeyError:
            sandbox = pwd.getpwnam('nobody')
        for directory in ('lists', 'archives'):
            path = APT_STATE / directory
            path.mkdir(mode=0o755, exist_ok=True)
            path.chmod(0o755)
            partial = path / 'partial'
            partial.mkdir(mode=0o700, exist_ok=True)
            os.chown(str(partial), sandbox.pw_uid, sandbox.pw_gid)
        replace_file(APT_STATE / 'cacert.pem', (PROJECT / 'certs/cacert.pem').read_bytes())
        sources = ('deb [arch=armhf check-valid-until=no signed-by={}] {} buster main\n'
                   'deb [arch=armhf check-valid-until=no signed-by={}] '
                   'https://archive.debian.org/debian-security buster/updates main\n').format(KEYRING, ARCHIVE, KEYRING)
        replace_file(APT_STATE / 'sources.list', sources.encode())
        preferences = ''.join('Package: {}\nPin: version {}\nPin-Priority: 1001\n\n'.format(pkg, version)
                              for pkg, version in sorted(original.items()))
        replace_file(APT_STATE / 'preferences', preferences.encode())
        options = {
            'Dir::Etc::main': '-', 'Dir::Etc::parts': '-', 'Dir::Etc::sourceparts': '-',
            'Dir::Etc::sourcelist': str(APT_STATE / 'sources.list'),
            'Dir::Etc::preferences': str(APT_STATE / 'preferences'), 'Dir::Etc::preferencesparts': '-',
            'Dir::Etc::trusted': str(KEYRING), 'Dir::Etc::trustedparts': '-',
            'Dir::State::lists': str(APT_STATE / 'lists'),
            'Dir::Cache::archives': str(APT_STATE / 'archives'),
            'Acquire::https::CaInfo': str(APT_STATE / 'cacert.pem'),
            'Acquire::Languages': 'none', 'APT::Architecture': 'armhf',
            'APT::Sandbox::User': sandbox.pw_name,
        }
        config = ''.join('{} {};\n'.format(key, json.dumps(value)) for key, value in options.items())
        replace_file(APT_STATE / 'apt.conf', config.encode())
        env = dict(os.environ, APT_CONFIG=str(APT_STATE / 'apt.conf'), DEBIAN_FRONTEND='noninteractive')
        run(['/usr/bin/apt-get', 'update'], env=env)
        arguments = ['/usr/bin/apt-get', '--no-install-recommends', '--no-remove', '--no-upgrade', 'install', 'avahi-daemon']
        plan = run(arguments[:1] + ['--simulate'] + arguments[1:], env=env).stdout
        new = apt_plan(plan, original)
        if 'avahi-daemon' not in new:
            raise RuntimeError('The APT plan does not install Avahi.')
        if inventory() != original:
            raise RuntimeError('The inventory changed while planning; rerun the script.')
        print('New packages: ' + ', '.join(new), flush=True)
        with block_package_services():
            run(arguments[:1] + ['--assume-yes'] + arguments[1:], env=env)
        after = inventory()
        if any(after.get(package) != version for package, version in original.items()):
            raise RuntimeError('An original package changed; stop and consult the backups.')
    config = Path('/etc/avahi/avahi-daemon.conf')
    text = config.read_text() if config.exists() else ''
    changed = replace_file(config, avahi_config(text, name).encode())
    run(['systemctl', 'enable', 'avahi-daemon.service'])
    if run(['systemctl', 'is-active', '--quiet', 'dbus.service'], check=False).returncode:
        run(['systemctl', 'start', 'dbus.service'])
    active = run(['systemctl', 'is-active', '--quiet', 'avahi-daemon.service'], check=False).returncode == 0
    if changed and active:
        run(['systemctl', 'restart', 'avahi-daemon.service'])
    elif not active:
        run(['systemctl', 'start', 'avahi-daemon.service'])
    run(['systemctl', 'is-active', '--quiet', 'avahi-daemon.service'])


def main():
    if sys.argv[1:] == ['--help']:
        print('Usage: sudo sh configure.sh\nPrompts for a hostname and configures DHCP, local name resolution, APT and Avahi.')
        return
    if len(sys.argv) != 1:
        raise ValueError('Unknown arguments; use --help.')
    if os.geteuid() != 0:
        raise RuntimeError('Run with sudo sh configure.sh.')
    os.umask(0o077)
    os.environ['PATH'] = '/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin'
    os.environ['LC_ALL'] = 'C'
    os.environ['SYSTEMD_PAGER'] = 'cat'
    release = Path('/etc/os-release').read_text()
    if not re.search(r'^ID=["\']?debian["\']?$', release, re.M) or not re.search(r'^VERSION_ID=["\']?10["\']?$', release, re.M):
        raise RuntimeError('This configurator supports only Debian 10 Buster.')
    if run(['dpkg', '--print-architecture']).stdout.strip() != 'armhf':
        raise RuntimeError('This configurator supports only armhf.')
    image = Path('/usr/local/etc/distro_version')
    if not image.exists() or not image.read_text().strip().startswith('ww-distro-dh-'):
        raise RuntimeError('The image does not match the known Watt&Well DH family.')
    for tool in ('hostname', 'getent', 'dpkg-query', 'dpkg-deb', 'gpgv', 'ip', 'systemctl', 'networkctl', 'dhclient', 'awk', 'ps'):
        if not shutil.which(tool):
            raise RuntimeError('Required tool is missing: ' + tool)
    if not KEYRING.is_file():
        raise RuntimeError('The trusted Debian keyring is missing; it will not be downloaded without authentication.')
    if not os.access('/sbin/dhclient-script', os.X_OK):
        raise RuntimeError('The existing DHCP hook is missing; it will not be replaced with a generic script.')
    if STATE.is_symlink():
        raise RuntimeError('The state directory cannot be a symlink.')
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    STATE.chmod(0o700)
    with (STATE / 'configure.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        restore_policy()
        current = Path('/etc/hostname').read_text().strip() or run(['hostname']).stdout.strip()
        while True:
            try:
                name = input('New hostname (Enter to keep {}): '.format(current)).strip() or current
            except EOFError:
                raise RuntimeError('No input: no device configuration was applied.')
            if valid_hostname(name):
                break
            print('Use 1-63 lowercase letters, digits or hyphens; no .local or leading/trailing hyphen.')
        packages = inventory(repair_libc=True)
        print('Configuring {}.local; backups in {}.'.format(name, STATE / 'backups'), flush=True)
        configure_name(name)
        configure_dhcp(name)
        print('Validating TLS with the configurator-specific certificate bundle...', flush=True)
        opener = downloader()
        if not apt_ready() or 'avahi-daemon:armhf' not in packages:
            download(opener, ARCHIVE + '/dists/buster/Release', STATE / 'connectivity.Release', limit=1024 * 1024)
        restore_apt(opener, packages)
        install_avahi(packages, name)
        print('Configuration complete. LAN name: {}.local'.format(name))
        print('DHCP is enabled for the next boot; the network and EVI services were not restarted.')
        if not Path('/var/lib/dpkg/info/libapt-pkg5.0:armhf.list').is_file():
            print('Warning: the libapt file list is still missing; it was not rebuilt automatically.')


if __name__ == '__main__':
    def interrupted(signum, frame):
        raise RuntimeError('Configuration interrupted by signal {}.'.format(signum))
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, interrupted)
    try:
        main()
    except (Exception, KeyboardInterrupt) as error:
        print('ERROR: {}'.format(error), file=sys.stderr)
        print('No general package repair or rollback was performed. Rerun after resolving the cause.', file=sys.stderr)
        sys.exit(1)
