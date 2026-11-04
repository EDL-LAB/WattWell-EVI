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
        raise ValueError('Hostname inválido.')
    lines = content.splitlines(keepends=True)
    matches = [i for i, line in enumerate(lines)
               if line.split('#', 1)[0].split()[:1] == ['127.0.1.1']]
    if len(matches) > 1:
        raise ValueError('Há várias entradas 127.0.1.1; não vou adivinhar qual alterar.')
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
        raise RuntimeError('Destino não é ficheiro regular: {}'.format(path))
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
            raise RuntimeError('Pacote em estado incompleto: {} {}'.format(name, status))
        packages[name + ':' + architecture] = version
    return packages


def inventory():
    return parse_packages(run(['dpkg-query', '-W',
        '-f=${Package}\t${Architecture}\t${Version}\t${Status}\n']).stdout)


def static_addresses(text):
    addresses = []
    for interface in json.loads(text):
        if interface['ifname'] != 'eth1':
            raise ValueError('Interface inesperada no inventário.')
        for entry in interface.get('addr_info', []):
            lifetime = entry.get('valid_life_time')
            if entry.get('family') != 'inet' or entry.get('scope') != 'global':
                continue
            if lifetime not in (4294967295, 'forever'):
                continue
            address = str(ipaddress.IPv4Interface('{}/{}'.format(entry['local'], entry['prefixlen'])))
            label = entry.get('label', 'eth1')
            if not re.fullmatch(r'eth1(?::[a-zA-Z0-9_-]{1,10})?', label):
                raise ValueError('Label IPv4 inesperado: {}'.format(label))
            addresses.append((address, label))
    return sorted(set(addresses))


def configure_dhcp(name):
    """Keep current leases; add a separate guarded client for subsequent boots."""
    unit = Path('/etc/systemd/system/wattwell-eth1-dhcp.service')
    if unit.exists() and not (STATE / 'static-addresses').is_file():
        raise RuntimeError('Falta o inventário IPv4 guardado para o serviço DHCP existente.')
    if not unit.exists():
        addresses = static_addresses(run(['ip', '-j', '-4', 'addr', 'show', 'dev', 'eth1']).stdout)
        if not addresses:
            raise RuntimeError('Não encontrei IPv4 permanentes na eth1; configuração desconhecida.')
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
            raise RuntimeError('Redirecionamento para transporte não autenticado recusado.')
        return super().redirect_request(request, fp, code, message, headers, new_url)


def downloader():
    bundle = PROJECT / 'certs' / 'cacert.pem'
    if hashlib.sha256(bundle.read_bytes()).hexdigest() != CA_SHA256:
        raise RuntimeError('O conjunto de certificados do projeto não corresponde ao hash esperado.')
    context = ssl.create_default_context(cafile=str(bundle))
    return urllib.request.build_opener(HTTPSRedirect(), urllib.request.HTTPSHandler(context=context))


def download(opener, url, path, expected=None, limit=100 * 1024 * 1024):
    if urllib.parse.urlsplit(url).scheme != 'https':
        raise ValueError('Apenas HTTPS é permitido.')
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
                raise ValueError('Download excede o limite previsto.')
            digest.update(chunk)
            target.write(chunk)
    if expected and digest.hexdigest() != expected:
        raise ValueError('SHA-256 incorreto: {}'.format(url))


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
                raise ValueError('Metadados Debian malformados.')
            fields[key] = value.strip()
    if fields:
        yield fields


def authenticated_apt_packages(opener, versions, directory):
    print('A obter a versão exata de APT num snapshot Debian autenticado...', flush=True)
    release = directory / 'Release'
    signature = directory / 'Release.gpg'
    download(opener, SNAPSHOT + '/dists/buster/Release', release, limit=1024 * 1024)
    download(opener, SNAPSHOT + '/dists/buster/Release.gpg', signature, limit=1024 * 1024)
    run(['gpgv', '--homedir', str(directory), '--keyring', str(KEYRING), str(signature), str(release)])
    metadata = next(paragraphs(release.read_text().splitlines()))
    if metadata.get('Codename') != 'buster':
        raise ValueError('O snapshot não é Buster.')
    index_name = 'main/binary-armhf/Packages.xz'
    checksum = None
    for line in metadata['SHA256'].splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[2] == index_name:
            checksum = parts[0]
    if not checksum or not re.fullmatch('[0-9a-f]{64}', checksum):
        raise ValueError('Falta o hash autenticado do índice armhf.')
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
                raise ValueError('Caminho de pacote inesperado.')
            sha = entry['SHA256']
            if not re.fullmatch('[0-9a-f]{64}', sha):
                raise ValueError('Hash de pacote inválido.')
            path = directory / (name + '.deb')
            download(opener, SNAPSHOT + '/' + str(filename), path, sha)
            found[name] = path
    if set(found) != set(wanted):
        raise RuntimeError('A versão instalada não está no snapshot suportado; nenhum upgrade foi feito.')
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
        raise RuntimeError('Recuperação APT limitada à versão registada 1.8.2.1; sem upgrade automático.')
    with tempfile.TemporaryDirectory(prefix='restore-apt-', dir=str(STATE)) as temporary:
        directory = Path(temporary)
        debs = authenticated_apt_packages(opener, versions, directory)
        for name, package in debs.items():
            fields = run(['dpkg-deb', '-f', str(package), 'Package', 'Version', 'Architecture']).stdout
            if ('Package: ' + name not in fields or 'Version: ' + versions[name] not in fields
                    or 'Architecture: armhf' not in fields):
                raise ValueError('Identidade do pacote não corresponde aos metadados.')
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
                    raise ValueError('Destino de recuperação fora de /usr.')
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
                        raise ValueError('Link de pacote fora de /usr.')
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
            raise RuntimeError('O plano APT remove pacotes; operação recusada.')
        if line.startswith(('Inst ', 'Conf ')):
            fields = line.split()
            name = fields[1].split(':', 1)[0]
            if name in installed_names:
                raise RuntimeError('O plano altera um pacote existente: ' + name)
            if fields[0] == 'Inst':
                new.append(name)
    return new


def restore_policy():
    journal = STATE / 'policy-journal.json'
    if not journal.exists():
        return
    if POLICY.is_symlink():
        raise RuntimeError('A política de serviços foi substituída por um link; preservar o journal.')
    record = json.loads(journal.read_text())
    original = STATE / 'policy-rc.d.original'
    current = POLICY.read_bytes() if POLICY.exists() else None
    old = original.read_bytes() if record['existed'] else None
    if current not in (DENY, old):
        raise RuntimeError('A política de serviços mudou; preservar e rever ' + str(journal))
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
        raise RuntimeError('policy-rc.d não é um ficheiro regular; não será substituído.')
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


def install_avahi(original, name):
    if 'avahi-daemon:armhf' not in original:
        print('A preparar instalação Avahi sem upgrades...', flush=True)
        if APT_STATE.is_symlink():
            raise RuntimeError('O estado APT não pode ser um link.')
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
            raise RuntimeError('O plano APT não instala Avahi.')
        if inventory() != original:
            raise RuntimeError('O inventário mudou durante o plano; repetir a execução.')
        print('Pacotes novos: ' + ', '.join(new), flush=True)
        with block_package_services():
            run(arguments[:1] + ['--assume-yes'] + arguments[1:], env=env)
        after = inventory()
        if any(after.get(package) != version for package, version in original.items()):
            raise RuntimeError('Um pacote original mudou; parar e consultar os backups.')
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
        print('Uso: sudo sh configure.sh\nPede o hostname e configura DHCP, resolução local, APT e Avahi.')
        return
    if len(sys.argv) != 1:
        raise ValueError('Argumentos desconhecidos; usar --help.')
    if os.geteuid() != 0:
        raise RuntimeError('Executar com sudo sh configure.sh.')
    os.umask(0o077)
    os.environ['PATH'] = '/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin'
    os.environ['LC_ALL'] = 'C'
    os.environ['SYSTEMD_PAGER'] = 'cat'
    release = Path('/etc/os-release').read_text()
    if not re.search(r'^ID=["\']?debian["\']?$', release, re.M) or not re.search(r'^VERSION_ID=["\']?10["\']?$', release, re.M):
        raise RuntimeError('Este configurador suporta apenas Debian 10 Buster.')
    if run(['dpkg', '--print-architecture']).stdout.strip() != 'armhf':
        raise RuntimeError('Este configurador suporta apenas armhf.')
    image = Path('/usr/local/etc/distro_version')
    if not image.exists() or not image.read_text().strip().startswith('ww-distro-dh-'):
        raise RuntimeError('A imagem não corresponde à família Watt&Well DH conhecida.')
    for tool in ('hostname', 'getent', 'dpkg-query', 'dpkg-deb', 'gpgv', 'ip', 'systemctl', 'networkctl', 'dhclient', 'awk', 'ps'):
        if not shutil.which(tool):
            raise RuntimeError('Ferramenta necessária ausente: ' + tool)
    if not KEYRING.is_file():
        raise RuntimeError('Falta o keyring Debian de confiança; não será descarregado sem autenticação.')
    if not os.access('/sbin/dhclient-script', os.X_OK):
        raise RuntimeError('Falta o hook DHCP existente; não será substituído por um script genérico.')
    if STATE.is_symlink():
        raise RuntimeError('Diretório de estado não pode ser um link.')
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    STATE.chmod(0o700)
    with (STATE / 'configure.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        restore_policy()
        packages = inventory()
        current = Path('/etc/hostname').read_text().strip() or run(['hostname']).stdout.strip()
        while True:
            try:
                name = input('Novo hostname (Enter para manter {}): '.format(current)).strip() or current
            except EOFError:
                raise RuntimeError('Sem entrada: nenhuma configuração de dispositivo foi aplicada.')
            if valid_hostname(name):
                break
            print('Usar 1–63 letras minúsculas/algarismos/hífens, sem .local ou hífen nas pontas.')
        print('A configurar {}.local; backups em {}.'.format(name, STATE / 'backups'), flush=True)
        configure_name(name)
        configure_dhcp(name)
        print('A validar TLS com os certificados privados do configurador...', flush=True)
        opener = downloader()
        if not apt_ready() or 'avahi-daemon:armhf' not in packages:
            download(opener, ARCHIVE + '/dists/buster/Release', STATE / 'connectivity.Release', limit=1024 * 1024)
        restore_apt(opener, packages)
        install_avahi(packages, name)
        print('Configuração concluída. Nome na LAN: {}.local'.format(name))
        print('DHCP fica habilitado para o próximo arranque; não foi reiniciada a rede nem serviços EVI.')
        if not Path('/var/lib/dpkg/info/libapt-pkg5.0:armhf.list').is_file():
            print('Aviso: a lista de ficheiros libapt continua ausente; não foi reconstruída automaticamente.')


if __name__ == '__main__':
    def interrupted(signum, frame):
        raise RuntimeError('Configuração interrompida pelo sinal {}.'.format(signum))
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, interrupted)
    try:
        main()
    except (Exception, KeyboardInterrupt) as error:
        print('ERRO: {}'.format(error), file=sys.stderr)
        print('Não foi efetuada reparação geral nem rollback de pacotes. Repetir após resolver a causa.', file=sys.stderr)
        sys.exit(1)
