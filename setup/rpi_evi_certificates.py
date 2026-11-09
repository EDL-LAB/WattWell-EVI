#!/usr/bin/env python3
"""Local interactive certificate assistant for Grid, EVI and RPi; Python 3.7+."""
import hashlib
import http.client
import ipaddress
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import time

STATE = Path('/var/lib/evi-mtls-setup')
DEVICE_DIRS = {'evi': Path('/etc/wattwell-evi/csm-https'), 'rpi': Path('/etc/evcharger/csm-tls')}
CLIENT_CN = 'evcharger-csm-client'
CLIENT_DNS = CLIENT_CN + '.local'


def run(*args):
    result = subprocess.run([str(arg) for arg in args], check=True, capture_output=True, timeout=30)
    return result.stdout


def dns(value):
    if len(value) > 253 or not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+', value):
        raise ValueError('Enter a lowercase DNS name such as evi3.local.')
    return value


def address(value):
    if value:
        return str(ipaddress.IPv4Address(value))
    return ''


def port(value):
    if not value.isdigit() or not 1 <= int(value) <= 65535:
        raise ValueError('Enter a port between 1 and 65535.')
    return int(value)


def count(value):
    if value not in ('1', '2'):
        raise ValueError('Choose 1 or 2 connectors.')
    return int(value)


def ask(label, default='', validator=lambda value: value):
    while True:
        value = input(label + (' [' + str(default) + ']' if default != '' else '') + ': ').strip()
        try:
            return validator(value if value else str(default))
        except ValueError as error:
            print(error)


def yes(label):
    return input(label + ' [y/N]: ').strip().lower() == 'y'


def profile(previous=None):
    defaults = {'evi_name': 'evi3.local', 'evi_ip': '', 'rpi_name': 'evcharger.local', 'rpi_ip': '',
                'ca_name': 'rpi-grid-controller.local', 'ca_ip': '', 'ca_port': 9000,
                'https_port': 8443, 'csm_port': 8444, 'evse_count': 1}
    defaults.update(previous or {})
    fields = [('evi_name', 'EVI DNS name', dns), ('evi_ip', 'EVI IPv4 (optional; Enter uses DNS)', address),
              ('rpi_name', 'RPi HMI DNS name', dns), ('rpi_ip', 'RPi HMI IPv4 (optional)', address),
              ('ca_name', 'Grid CA DNS name', dns), ('ca_ip', 'Grid CA IPv4 (optional)', address),
              ('ca_port', 'Grid CA HTTPS port', port), ('https_port', 'EVI HTTPS port', port),
              ('csm_port', 'EVI local CSM HTTP port', port), ('evse_count', 'Number of EVSE connectors', count)]
    return {name: ask(label, defaults[name], validator) for name, label, validator in fields}


def validate_profile(data):
    if not isinstance(data, dict):
        raise ValueError('Expected connection settings.')
    for name in ('evi_name', 'rpi_name', 'ca_name'):
        dns(data[name])
    for name in ('evi_ip', 'rpi_ip', 'ca_ip'):
        address(data[name])
    for name in ('ca_port', 'https_port', 'csm_port'):
        if not isinstance(data[name], int): raise ValueError('Expected an integer port.')
        port(str(data[name]))
    count(str(data['evse_count']))
    return data


def read_json(path):
    with path.open('rb') as source:
        raw = source.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise ValueError('Exchange file exceeds 1 MiB.')
    data = json.loads(raw)
    if data.get('version') != 1 or data.get('kind') not in ('request', 'certificate') or data.get('role') not in ('server', 'client'):
        raise ValueError('Unsupported exchange file.')
    validate_profile(data['profile'])
    return data


def write(path, content, mode=0o600, backup=False):
    path = Path(path)
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError('Destination is not a regular file: ' + str(path))
    if path.exists() and path.read_bytes() == content:
        return
    if path.exists() and not backup:
        raise ValueError('A different file already exists: ' + str(path))
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.exists():
        fd, saved = tempfile.mkstemp(prefix=path.name + '.backup-', dir=str(path.parent))
        os.close(fd)
        shutil.copy2(str(path), saved)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'wb') as output:
            output.write(content)
            output.flush()
            os.fchmod(output.fileno(), mode)
            os.fsync(output.fileno())
        os.replace(temporary, str(path))
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


def json_bytes(value):
    return (json.dumps(value, indent=2) + '\n').encode()


def public_path(filename):
    user = os.environ.get('SUDO_USER') or pwd.getpwuid(os.getuid()).pw_name
    account = pwd.getpwnam(user)
    directory = Path(account.pw_dir) / 'evi-certificates'
    if not directory.exists():
        directory.mkdir(mode=0o700)
        os.chown(str(directory), account.pw_uid, account.pw_gid)
    return directory / filename


def review(data):
    print('\nConnection settings:')
    print(json.dumps(data, indent=2))
    print('IP values are connection records; this stage does not change interfaces, DNS or routing.')
    print('The RPi certificate identity is ' + CLIENT_CN + ', independent of its network hostname.')


def identity(role, data):
    return (data['evi_name'], data['evi_name'], 'serverAuth') if role == 'server' else (CLIENT_CN, CLIENT_DNS, 'clientAuth')


def prepare_identity(directory, role, data):
    if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
        raise ValueError('Expected a private TLS directory.')
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    if directory.stat().st_uid != os.geteuid(): raise ValueError('TLS directory has another owner.')
    directory.chmod(0o700)
    name = 'server' if role == 'server' else 'client'
    key, csr = directory / (name + '.key'), directory / (name + '.csr')
    for path in (key, csr):
        if path.is_symlink() or (path.exists() and not path.is_file()): raise ValueError('TLS files must be regular.')
    if key.exists() != csr.exists(): raise ValueError('Incomplete key/CSR pair; preserve it for review.')
    cn, san, purpose = identity(role, data)
    if not key.exists():
        with tempfile.TemporaryDirectory(prefix='.request-', dir=str(directory)) as temporary:
            temporary = Path(temporary)
            run('openssl', 'req', '-new', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:prime256v1', '-nodes', '-sha256',
                '-keyout', temporary / 'key', '-out', temporary / 'csr', '-subj', '/CN=' + cn,
                '-addext', 'subjectAltName=DNS:' + san, '-addext', 'extendedKeyUsage=' + purpose)
            (temporary / 'key').chmod(0o600)
            os.link(str(temporary / 'key'), str(key))
            os.link(str(temporary / 'csr'), str(csr))
    if key.stat().st_uid != os.geteuid() or key.stat().st_mode & 0o777 != 0o600:
        raise ValueError('Private key must belong to this user and have mode 0600.')
    run('openssl', 'req', '-in', csr, '-verify', '-noout')
    if run('openssl', 'req', '-in', csr, '-subject', '-noout', '-nameopt', 'RFC2253').decode().strip() != 'subject=CN=' + cn:
        raise ValueError('Existing CSR belongs to another identity.')
    if run('openssl', 'pkey', '-in', key, '-passin', 'pass:', '-pubout') != run('openssl', 'req', '-in', csr, '-pubkey', '-noout'):
        raise ValueError('Private key and CSR do not match.')
    return key, csr


def request_device(device, data):
    role = 'server' if device == 'evi' else 'client'
    _, csr = prepare_identity(DEVICE_DIRS[device], role, data)
    bundle = {'version': 1, 'kind': 'request', 'role': role, 'profile': data, 'csr': csr.read_text()}
    fingerprint = hashlib.sha256(csr.read_bytes()).hexdigest()
    output = ask('Public request file', str(public_path(data['evi_name'] + '-' + role + '-request.json')), lambda v: Path(v).expanduser())
    write(output, json_bytes(bundle), 0o644)
    print('Public request saved: ' + str(output))
    print('CSR SHA-256: ' + fingerprint)
    print('Transfer only this public JSON to the Grid through a trusted channel; compare the fingerprint there.')
    print('The private key stays on this device. No service changed.')


def verify_certificate(chain, key, root, role, data):
    cn, san, purpose = identity(role, data)
    run('openssl', 'verify', '-x509_strict', '-purpose', 'sslserver' if purpose == 'serverAuth' else 'sslclient',
        '-verify_hostname', san, '-CAfile', root, '-untrusted', chain, chain)
    if run('openssl', 'x509', '-in', chain, '-subject', '-noout', '-nameopt', 'RFC2253').decode().strip() != 'subject=CN=' + cn:
        raise ValueError('Certificate subject differs from the expected identity.')
    if run('openssl', 'x509', '-in', chain, '-pubkey', '-noout') != run('openssl', 'pkey', '-in', key, '-passin', 'pass:', '-pubout'):
        raise ValueError('Certificate does not match the private key on this device.')
    text = run('openssl', 'x509', '-in', chain, '-text', '-noout').decode()
    for label, expected in [('Subject Alternative Name', 'DNS:' + san),
                            ('Extended Key Usage', 'TLS Web Server Authentication' if purpose == 'serverAuth' else 'TLS Web Client Authentication'),
                            ('Basic Constraints', 'CA:FALSE')]:
        match = re.search('X509v3 ' + label + r':[^\n]*\n\s*([^\n]+)', text)
        if not match or match.group(1).strip() != expected:
            raise ValueError('Unexpected certificate ' + label + '.')


def root_fingerprint(root):
    pem = root.read_text()
    if not re.fullmatch(r'\s*-----BEGIN CERTIFICATE-----\s+[A-Za-z0-9+/=\s]+-----END CERTIFICATE-----\s*', pem):
        raise ValueError('Expected exactly one public root CA certificate, without additional trust material.')
    return hashlib.sha256(run('openssl', 'x509', '-in', root, '-outform', 'DER')).hexdigest()


def install_device(device, local_profile):
    path = ask('Public response JSON from Grid', validator=lambda value: Path(value).expanduser())
    bundle = read_json(path)
    expected_role = 'server' if device == 'evi' else 'client'
    if bundle['kind'] != 'certificate' or bundle['role'] != expected_role or bundle['profile'] != local_profile:
        raise ValueError('The response role/settings do not match this device profile.')
    directory = DEVICE_DIRS[device]
    stem = 'server' if expected_role == 'server' else 'client'
    if not (directory / (stem + '.key')).is_file() or not (directory / (stem + '.csr')).is_file():
        raise ValueError('Prepare the local request before installing a response; no new key was generated.')
    key, csr = prepare_identity(directory, expected_role, local_profile)
    if hashlib.sha256(csr.read_bytes()).hexdigest() != bundle['csr_sha256']:
        raise ValueError('Response belongs to another CSR.')
    with tempfile.TemporaryDirectory(prefix='.verify-', dir=str(directory)) as temporary:
        temporary = Path(temporary)
        root, chain = temporary / 'root.pem', temporary / 'chain.pem'
        root.write_text(bundle['root_pem'])
        chain.write_text(bundle['chain_pem'])
        fingerprint = root_fingerprint(root)
        previous_root = directory / 'root-ca.pem'
        if previous_root.exists():
            if previous_root.is_symlink() or root_fingerprint(previous_root) != fingerprint:
                raise ValueError('CA root changed; preserve the current trust and review separately.')
        else:
            print('CA root SHA-256: ' + fingerprint)
            entered = ask('Enter the CA fingerprint independently verified on the Grid').replace(':', '').lower()
            if entered != fingerprint: raise ValueError('CA fingerprint does not match.')
        verify_certificate(chain, key, root, expected_role, local_profile)
        if not yes('Install the verified certificates, preserving the private key and existing service state?'): return
        write(directory / ('server-chain.pem' if device == 'evi' else 'client-chain.pem'), chain.read_bytes(), 0o644, backup=True)
        write(previous_root, root.read_bytes(), 0o644)
        if device == 'evi': write(directory / 'client-ca.pem', root.read_bytes(), 0o644, backup=True)
    print('Certificates installed. No service was started, restarted or enabled.')
    print('HTTPS activation, RPi client configuration, access restrictions and automatic renewal are still required.')


def grid(data):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
    import configure_evi_certificates as ca
    if ca.CONFIG.is_symlink(): raise ValueError('CA configuration cannot be a symlink.')
    config = json.loads(ca.CONFIG.read_text())
    if config['authority'].get('enableAdmin'): raise ValueError('Admin-managed CA: this file-based workflow is refused.')
    action = ask('1: prepare CA identities; 2: sign a public device request', '1', lambda v: v if v in ('1', '2') else invalid())
    names = ca.identities(data['evi_name'])
    step = shutil.which('step')
    if not step: raise ValueError('The Grid needs its existing Smallstep CLI.')
    if action == '1':
        print('This adds only the EVI/RPi scoped provisioners and required exact name allowances.')
        if yes('Apply the reviewed CA configuration with a protected backup?'):
            before = ca.CONFIG.read_bytes()
            ca.setup(config, names, step)
            if ca.CONFIG.read_bytes() != before:
                if yes('Restart only step-ca now to load the new rules? Issuance/renewal will briefly pause.'):
                    run('systemctl', 'restart', 'step-ca.service')
                    root_path = Path(config['root'][0] if isinstance(config['root'], list) else config['root'])
                    context = ssl.create_default_context(cafile=str(root_path))
                    for attempt in range(5):
                        connection = http.client.HTTPSConnection(data['ca_name'], data['ca_port'], context=context, timeout=3)
                        try:
                            connection.request('GET', '/health')
                            response = connection.getresponse()
                            if response.status == 200 and json.loads(response.read(1024)).get('status') == 'ok':
                                print('CA HTTPS health verified.')
                                break
                        except (OSError, ValueError, http.client.HTTPException):
                            if attempt == 4: raise ValueError('CA health is not ready. Existing CA backup was retained; inspect before signing.')
                        finally:
                            connection.close()
                        time.sleep(1)
                    else:
                        raise ValueError('CA health did not return OK; inspect before signing.')
                else:
                    print('Load the CA configuration before signing; no service was restarted.')
        return
    path = ask('Public device request JSON', validator=lambda v: Path(v).expanduser())
    request = read_json(path)
    if request['kind'] != 'request' or request['profile'] != data:
        raise ValueError('Request settings differ. Use the same saved profile on all devices.')
    csr_bytes = request['csr'].encode('ascii')
    if len(csr_bytes) > 64 * 1024: raise ValueError('CSR exceeds 64 KiB.')
    csr = ca.x509.load_pem_x509_csr(csr_bytes)
    ca.validate_identity(csr, names[request['role']])
    print('CSR SHA-256: ' + hashlib.sha256(csr_bytes).hexdigest())
    print('Role: ' + request['role'] + '; identity: ' + names[request['role']][1])
    if not yes('Does this fingerprint match the target device over your trusted handoff, and issue now?'): return
    root_path = Path(config['root'][0] if isinstance(config['root'], list) else config['root'])
    with tempfile.TemporaryDirectory(prefix='evi-wizard-') as temporary:
        destination = Path(temporary) / 'chain.pem'
        ca.issue(config, names[request['role']], csr_bytes, destination,
                 'https://' + data['ca_name'] + ':' + str(data['ca_port']), step)
        response = {'version': 1, 'kind': 'certificate', 'role': request['role'], 'profile': data,
                    'csr_sha256': hashlib.sha256(csr_bytes).hexdigest(),
                    'root_pem': root_path.read_text(), 'chain_pem': destination.read_text()}
        output = ask('Public response file', str(public_path(data['evi_name'] + '-' + request['role'] + '-certificate.json')),
                     lambda v: Path(v).expanduser())
        write(output, json_bytes(response), 0o644, backup=True)
    fingerprint = root_fingerprint(root_path)
    print('Public response: ' + str(output))
    print('CA root SHA-256 for independent verification on the target: ' + fingerprint)
    print('Transfer only this public JSON to the target and choose Install response in its local assistant.')


def invalid():
    raise ValueError('Choose one of the displayed numbers.')


def main():
    if sys.argv[1:]: raise ValueError('Run without arguments; this assistant asks for its settings.')
    if not sys.stdin.isatty(): raise ValueError('An interactive terminal is required.')
    if os.geteuid() != 0: raise ValueError('Run this local assistant with sudo.')
    os.umask(0o077)
    print('RPi -> EVI: mTLS certificate assistant. Certificates only; no SSH or service activation.')
    scripts = Path(__file__).resolve().parents[1] / 'scripts'
    device = 'grid' if (scripts / 'configure_evi_certificates.py').is_file() else ('evi' if (scripts / 'prepare-csm-https.py').is_file() else 'rpi')
    print('Repository role: ' + device)
    saved = STATE / device / 'profile.json'
    previous = json.loads(saved.read_text()) if saved.exists() and not saved.is_symlink() else None
    shared = ask('Existing public request/response JSON to load settings (optional)')
    if shared:
        previous = read_json(Path(shared).expanduser())['profile']
    data = profile(previous)
    validate_profile(data)
    review(data)
    if not yes('Save these settings and continue?'): return
    write(saved, json_bytes(data), backup=True)
    if device == 'grid':
        grid(data)
    else:
        action = ask('1: prepare/export public request; 2: install Grid response', '1', lambda v: v if v in ('1', '2') else invalid())
        if action == '1': request_device(device, data)
        else: install_device(device, data)


if __name__ == '__main__':
    try:
        main()
    except (EOFError, KeyboardInterrupt):
        raise SystemExit('Cancelled. Existing private keys and services were preserved.')
    except ValueError as error:
        raise SystemExit('ERROR: ' + str(error))
    except Exception:
        raise SystemExit('Operation failed. Check public files, identities, CA connectivity and permissions. Private/error output suppressed.')
