#!/usr/bin/env python3
"""Commission the local EVI proxy or RPi CSM client; never start charging services."""
import argparse
import http.client
import json
import os
from pathlib import Path
import pwd
import re
import socket
import ssl
import subprocess
import sys
import tempfile

import rpi_evi_certificates as pki

REPO = Path(__file__).resolve().parents[1]
RUNTIME = Path('/etc/evcharger/csm-https')
SERVICE = Path('/etc/systemd/system/wattwell-csm-https.service')
CSM_CONFIG = Path('/usr/local/etc/evis-csm/evis-csm.toml')
CADDY = Path('/usr/local/bin/caddy')
MARKER = '# Managed by WattWell-EVI setup/csm-https.sh\n'


def protected(path, directory=False):
    path = Path(path)
    for parent in (path,) + tuple(path.parents):
        if parent.is_symlink():
            raise ValueError('Symlink in protected path: ' + str(parent))
    info = path.stat()
    if (not path.is_dir() if directory else not path.is_file()) or info.st_uid != 0 or info.st_mode & 0o022:
        raise ValueError('Expected a root-owned path without group/other write access: ' + str(path))
    return path


def material(device, data):
    directory = protected(pki.DEVICE_DIRS[device], directory=True)
    stem = 'server' if device == 'evi' else 'client'
    key = protected(directory / (stem + '.key'))
    if key.stat().st_mode & 0o077:
        raise ValueError('The enrollment private key must remain root-only.')
    chain = protected(directory / (stem + '-chain.pem'))
    root = protected(directory / 'root-ca.pem')
    pki.root_fingerprint(root)
    pki.verify_certificate(chain, key, root, stem, data)
    try:
        pki.run('openssl', 'x509', '-in', chain, '-checkend', '3600', '-noout')
    except subprocess.CalledProcessError:
        raise ValueError('Certificate expires within one hour; re-enroll through the Grid certificate assistant first.')
    print('Certificate: ' + pki.run('openssl', 'x509', '-in', chain, '-enddate', '-noout').decode().strip())
    return directory, chain, key, root


def csm_field(text, section, name):
    sections = re.findall(r'(?ms)^\[' + section + r'\]\s*\n(.*?)(?=^\[|\Z)', text)
    values = re.findall(r'(?m)^' + name + r'\s*=\s*([^\n#]+)', sections[0]) if len(sections) == 1 else []
    if len(values) != 1:
        raise ValueError('Expected one CSM ' + section + '.' + name + ' setting.')
    return values[0].strip().strip('\"\'')


def check_csm(data):
    # The vendor canonical link is allowed only inside its own configuration directory.
    resolved = CSM_CONFIG.resolve(strict=True)
    if resolved.parent != CSM_CONFIG.parent.resolve():
        raise ValueError('Unexpected CSM configuration link destination.')
    text = protected(resolved).read_text()
    if (csm_field(text, 'http', 'enabled') != 'true' or csm_field(text, 'http', 'ip') != '127.0.0.1'
            or csm_field(text, 'http', 'port') != str(data['csm_port'])
            or csm_field(text, 'modbus', 'enabled') != 'false'):
        raise ValueError('Run configure-csm.sh --apply first: use loopback HTTP with the saved CSM port and disabled Modbus.')
    status = pki.run('systemctl', 'show', 'evis-csm.service', '-p', 'LoadState', '-p', 'ActiveState').decode().splitlines()
    if 'LoadState=loaded' not in status or not any('ActiveState=' + state in status for state in ('inactive', 'failed')):
        raise ValueError('CSM must already be stopped before proxy commissioning; it was not stopped automatically.')


def proxy_config(data):
    example = (REPO / 'config/csm-https.Caddyfile.example').read_text()
    return MARKER + example.replace('https://evi3.local:8443', 'https://' + data['evi_name'] + ':' + str(data['https_port'])).replace(
        '127.0.0.1:8444', '127.0.0.1:' + str(data['csm_port']))


def proxy_unit():
    return MARKER + '''[Unit]
Description=WattWell CSM HTTPS/mTLS proxy
After=network-online.target evis-csm.service
Wants=network-online.target

[Service]
Type=simple
User=root
ExecStart=/usr/local/bin/caddy run --config /etc/wattwell-evi/csm-https/Caddyfile --adapter caddyfile
Restart=on-failure
RestartSec=5
UMask=0077
RuntimeDirectory=wattwell-csm-https
RuntimeDirectoryMode=0700
Environment=XDG_DATA_HOME=/run/wattwell-csm-https
Environment=XDG_CONFIG_HOME=/run/wattwell-csm-https
NoNewPrivileges=true
PrivateTmp=true
PrivateDevices=true
ProtectSystem=strict
ProtectHome=true
CapabilityBoundingSet=
RestrictSUIDSGID=true

[Install]
WantedBy=multi-user.target
'''


def managed_destination(path):
    if path.exists() or path.is_symlink():
        protected(path)
        if not path.read_text().startswith(MARKER):
            raise ValueError('An unmanaged file was preserved: ' + str(path))


def evi(data, apply):
    check_csm(data)
    directory, chain, key, root = material('evi', data)
    if pki.root_fingerprint(protected(directory / 'client-ca.pem')) != pki.root_fingerprint(root):
        raise ValueError('Client trust differs from the enrolled Grid root.')
    protected(CADDY)
    if not pki.run(CADDY, 'version').startswith(b'v2.11.9 '):
        raise ValueError('Prepare the verified Caddy 2.11.9 binary first.')
    config = proxy_config(data)
    destination = directory / 'Caddyfile'
    for path in (destination, SERVICE):
        managed_destination(path)
    with tempfile.TemporaryDirectory(prefix='.proxy-check-', dir=str(directory)) as temporary:
        candidate = Path(temporary) / 'Caddyfile'
        candidate.write_text(config)
        pki.run(CADDY, 'validate', '--config', candidate, '--adapter', 'caddyfile')
    print('Proxy: https://' + data['evi_name'] + ':' + str(data['https_port']) + ' -> 127.0.0.1:' + str(data['csm_port']))
    if not apply:
        print('Preview only. Use --apply to install the validated proxy configuration and unit.')
        return
    if not pki.yes('Install this proxy configuration and service, with backups; keep CSM stopped?'):
        return
    pki.write(destination, config.encode(), backup=True)
    pki.write(SERVICE, proxy_unit().encode(), 0o644, backup=True)
    pki.run('systemctl', 'daemon-reload')
    if pki.yes('Enable and start/restart ONLY wattwell-csm-https.service now?'):
        pki.run('systemctl', 'enable', SERVICE.name)
        pki.run('systemctl', 'restart', SERVICE.name)
        pki.run('systemctl', 'is-active', '--quiet', SERVICE.name)
        print('HTTPS proxy is active. CSM was not started; upstream reads return 502 until CSM is commissioned.')
    else:
        print('Proxy files installed; the running proxy state was preserved. Activation is still required.')


def backend_account():
    default = os.environ.get('SUDO_USER', '')
    result = subprocess.run(['systemctl', 'show', 'evcharger-backend.service', '-p', 'LoadState', '-p', 'User', '-p', 'WorkingDirectory'],
                            capture_output=True, text=True, timeout=30)
    values = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
    if result.returncode and values.get('LoadState') != 'not-found':
        raise ValueError('Cannot inspect the existing backend service; no runtime configuration was changed.')
    if values.get('LoadState') == 'loaded':
        working = values.get('WorkingDirectory', '')
        if not working or Path(working).resolve() not in (REPO, REPO / 'backend'):
            raise ValueError('evcharger-backend.service uses WorkingDirectory=' + (working or '<unset>')
                             + '; this assistant is in ' + str(REPO)
                             + '. Update/build the installed checkout and run its setup/csm-https.sh --apply. No client configuration was installed.')
        default = values.get('User', '')
    user = pki.ask('Existing backend operating-system user', default)
    account = pwd.getpwnam(user)
    if account.pw_uid == 0 or (values.get('LoadState') == 'loaded' and user != default):
        raise ValueError('Use the existing non-root backend service user.')
    return account


def rpi(data, apply):
    _, chain, key, root = material('rpi', data)
    account = backend_account()
    for name, marker in (('CSMTLS.js', 'connection.json'), ('RealCSMService.js', 'rejectUnauthorized: true')):
        path = REPO / 'backend/dist/services/csm' / name
        if not path.is_file() or marker not in path.read_text():
            raise ValueError('Build the updated checkout first: npm run build:shared && npm run build:backend')
    if RUNTIME.exists() or RUNTIME.is_symlink():
        protected(RUNTIME, directory=True)
        if not (RUNTIME / 'connection.json').is_file():
            raise ValueError('An existing unmanaged runtime directory was preserved.')
    config = {'host': data['evi_name'], 'port': data['https_port']}
    if data['evi_ip']:
        config['address'] = data['evi_ip']
    print('RPi CSM: HTTPS/mTLS, verified DNS identity ' + data['evi_name'] + '; backend user ' + account.pw_name)
    print('The managed connection selects real CSM mode on the next backend start. RFID/whitelist settings are preserved.')
    if not apply:
        print('Preview only. Use --apply to install the protected client runtime configuration.')
        return
    if not pki.yes('Install the mTLS client connection for the existing backend, without restarting it?'):
        return
    RUNTIME.mkdir(mode=0o750, parents=True, exist_ok=True)
    os.chown(str(RUNTIME), 0, account.pw_gid)
    RUNTIME.chmod(0o750)
    for name, source in (('client-chain.pem', chain), ('client.key', key), ('root-ca.pem', root)):
        destination = RUNTIME / name
        pki.write(destination, source.read_bytes(), 0o640, backup=True)
        os.chown(str(destination), 0, account.pw_gid)
        destination.chmod(0o640)
    config_path = RUNTIME / 'connection.json'
    pki.write(config_path, pki.json_bytes(config), 0o640, backup=True)
    os.chown(str(config_path), 0, account.pw_gid)
    config_path.chmod(0o640)
    for path in (config_path, RUNTIME / 'client.key', RUNTIME / 'client-chain.pem', RUNTIME / 'root-ca.pem'):
        pki.run('runuser', '-u', account.pw_name, '--', 'test', '-r', path)
    print('Client configured. No backend, frontend, whitelist or charging service was restarted.')
    print('After checking the equipment, restart only the existing backend to load this transport.')


def read_status(data, context):
    with socket.create_connection((data['evi_ip'] or data['evi_name'], data['https_port']), timeout=5) as tcp:
        with context.wrap_socket(tcp, server_hostname=data['evi_name']) as secured:
            connection = http.client.HTTPConnection(data['evi_name'], data['https_port'], timeout=5)
            connection.sock = secured
            connection.request('GET', '/api/v1/csm/status', headers={'Host': data['evi_name']})
            response = connection.getresponse()
            return response.status


def check_rpi(data):
    if not RUNTIME.exists() or not (RUNTIME / 'connection.json').exists():
        raise ValueError('The mTLS client runtime is not installed. Run setup/csm-https.sh --apply from the backend checkout first.')
    protected(RUNTIME, directory=True)
    config = json.loads(protected(RUNTIME / 'connection.json').read_text())
    if (config.get('host') != data['evi_name'] or config.get('port') != data['https_port']
            or config.get('address', '') != data['evi_ip']):
        raise ValueError('Runtime connection differs from the enrollment profile; rerun --apply.')
    context = ssl.create_default_context(cafile=str(protected(RUNTIME / 'root-ca.pem')))
    context.load_cert_chain(str(protected(RUNTIME / 'client-chain.pem')), str(protected(RUNTIME / 'client.key')))
    status = read_status(data, context)
    if status not in (200, 502, 503):
        raise ValueError('The dedicated client did not reach the expected proxy route; HTTP ' + str(status))
    anonymous = ssl.create_default_context(cafile=str(RUNTIME / 'root-ca.pem'))
    try:
        read_status(data, anonymous)
    except ssl.SSLError:
        pass
    else:
        raise ValueError('The endpoint accepted a request without a client certificate.')
    try:
        direct = socket.create_connection((data['evi_ip'] or data['evi_name'], data['csm_port']), timeout=3)
    except (ConnectionRefusedError, socket.timeout):
        pass
    else:
        direct.close()
        raise ValueError('Direct CSM HTTP is reachable from the RPi; close this bypass before commissioning.')
    print('Verified server identity and CA, dedicated client access, rejection without a client certificate and blocked direct HTTP.')
    print('CSM status: HTTP ' + str(status) + ('.' if status == 200 else '; proxy TLS works, but the CSM upstream is unavailable.'))
    print('Read-only test; no RFID authorization, power, start or stop command was sent.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--apply', action='store_true')
    action.add_argument('--check', action='store_true', help='RPi only: read-only mTLS and direct-HTTP checks')
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise ValueError('Run this local assistant with sudo.')
    os.umask(0o077)
    device = 'evi' if (REPO / 'scripts/prepare-csm-https.py').is_file() else 'rpi'
    saved = protected(pki.STATE / device / 'profile.json')
    data = pki.validate_profile(json.loads(saved.read_text()))
    if data['https_port'] < 1024:
        raise ValueError('Use an HTTPS port above 1023 for the restricted proxy service.')
    if data['https_port'] == data['csm_port']:
        raise ValueError('The HTTPS proxy and local CSM must use different ports.')
    print('Local CSM HTTPS assistant; role: ' + device + '. No automatic CSM or backend activation.')
    if args.check:
        if device != 'rpi':
            raise ValueError('Run --check from the RPi after configuring both endpoints.')
        check_rpi(data)
    elif device == 'evi':
        evi(data, args.apply)
    else:
        rpi(data, args.apply)
    print('For automatic renewal or expired-certificate recovery, follow docs/csm-certificate-renewal.md.')


if __name__ == '__main__':
    try:
        main()
    except (EOFError, KeyboardInterrupt):
        raise SystemExit('Cancelled. No charging service was started.')
    except ValueError as error:
        raise SystemExit('ERROR: ' + str(error))
    except Exception:
        raise SystemExit('Operation failed. Check certificates/expiry, saved profile, permissions and service state. Private/error output suppressed.')
