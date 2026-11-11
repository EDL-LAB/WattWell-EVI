#!/usr/bin/env python3
"""Renew the local CSM TLS identity; never restart charging services. Python 3.7+."""
import argparse
import base64
import fcntl
import hashlib
import http.client
import json
import os
from pathlib import Path
import platform
import shutil
import socket
import ssl
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import urllib.error

import csm_https as https
import rpi_evi_certificates as pki

# Vendor /usr/local/lib may be writable by non-root; keep root timer code in protected setup state.
INSTALL = pki.STATE / 'renewal-runtime'
UNITS = Path('/etc/systemd/system')
NAME = 'wattwell-csm-renewal'
MARKER = '# Managed by setup/csm-renewal.sh\n'
VERSION = '0.30.6'
ARCHIVES = {
    'armv7l': ('armv7', 'bb80c0afda05c30f5ab8fd414fcde0037e341fbc75e68e025a6b09e02081b201'),
    'aarch64': ('arm64', 'eff511c3e6797039702e74fada62b10b079e413742f925703e5b7d810e611619'),
    'x86_64': ('amd64', 'e44a5dc5f880a694b24a0f2941a69a81b0bc6ee053170fdfde18453d4d5816de'),
}


def expiry(chain):
    text = pki.run('openssl', 'x509', '-in', chain, '-enddate', '-noout').decode().strip()
    return ssl.cert_time_to_seconds(text.split('=', 1)[1])


def identity(device, data):
    directory = https.protected(pki.DEVICE_DIRS[device], directory=True)
    role = 'server' if device == 'evi' else 'client'
    chain = https.protected(directory / (role + '-chain.pem'))
    key = https.protected(directory / (role + '.key'))
    root = https.protected(directory / 'root-ca.pem')
    if key.stat().st_mode & 0o077:
        raise ValueError('The identity private key must remain root-only.')
    if expiry(chain) <= time.time():
        raise ValueError('Certificate expired. Reissue with setup/rpi-evi-certificates.sh option 3, then rerun this assistant.')
    pki.root_fingerprint(root)
    pki.verify_certificate(chain, key, root, role, data)
    return directory, role, chain, key, root


def request_renewal(data, role, chain, key, root, step):
    url = 'https://' + data['ca_name'] + ':' + str(data['ca_port'])
    if role == 'client':
        token = pki.run(step, 'ca', 'token', pki.identity(role, data)[0],
                        '--renew', '--x5c-cert', chain, '--x5c-key', key,
                        '--ca-url', url, '--root', root, '--not-after', '5m').decode().strip()
        endpoint, payload, headers = '/1.0/renew', None, {'Authorization': 'Bearer ' + token}
    else:
        # Native Smallstep renewal requires clientAuth; the Grid instead trusts this exact device public key.
        # Step's key formatter accepts one PEM block, not the full leaf/issuer chain.
        with tempfile.TemporaryDirectory(prefix='.renew-public-', dir=str(chain.parent)) as temporary:
            public_key = Path(temporary) / 'public.pem'
            public_key.write_bytes(pki.run('openssl', 'x509', '-in', chain, '-pubkey', '-noout'))
            public = json.loads(pki.run(step, 'crypto', 'key', 'format', '--jwk', public_key))
        core = {name: public[name] for name in ('crv', 'kty', 'x', 'y')}
        kid = base64.urlsafe_b64encode(hashlib.sha256(json.dumps(core, sort_keys=True, separators=(',', ':')).encode()).digest()).rstrip(b'=').decode()
        csr = https.protected(key.parent / 'server.csr')
        pki.run('openssl', 'req', '-in', csr, '-verify', '-noout')
        if pki.run('openssl', 'req', '-in', csr, '-pubkey', '-noout') != pki.run('openssl', 'pkey', '-in', key, '-pubout'):
            raise ValueError('Server CSR does not match the current private key.')
        token = pki.run(step, 'ca', 'token', data['evi_name'], '--offline',
                        '--issuer', 'evi-csm-renew-' + data['evi_name'].replace('.', '-'),
                        '--kid', kid, '--key', key, '--san', data['evi_name'], '--cnf-file', csr,
                        '--ca-url', url, '--root', root, '--not-after', '5m').decode().strip()
        endpoint = '/1.0/sign'
        payload = json.dumps({'csr': csr.read_text(), 'ott': token})
        headers = {'Content-Type': 'application/json'}
    context = ssl.create_default_context(cafile=str(root))
    connection = http.client.HTTPConnection(data['ca_name'], data['ca_port'], timeout=15)
    raw = socket.create_connection((data['ca_ip'] or data['ca_name'], data['ca_port']), timeout=15)
    try:
        connection.sock = context.wrap_socket(raw, server_hostname=data['ca_name'])
        connection.request('POST', endpoint, payload, headers)
        response = connection.getresponse()
        body = response.read(1024 * 1024 + 1)
        if response.status != 201 or len(body) > 1024 * 1024:
            raise ValueError('CA renewal failed (HTTP ' + str(response.status) + '); existing certificates were preserved. Check the local CA health/policy.')
        signed = json.loads(body)
        return (signed['crt'].rstrip() + '\n' + signed['ca'].rstrip() + '\n').encode('ascii')
    finally:
        connection.close()
        raw.close()


def refresh_evi(data, chain, key, root, state):
    directory = chain.parent
    config = https.protected(directory / 'Caddyfile')
    unit = https.protected(https.SERVICE)
    if config.read_text() != https.proxy_config(data) or unit.read_text() != https.proxy_unit():
        raise ValueError('The managed proxy configuration/unit differs; review before refreshing certificates.')
    if not pki.run(https.CADDY, 'version').startswith(b'v2.11.9 '):
        raise ValueError('Certificate reload requires the reviewed Caddy 2.11.9 binary.')
    pki.run('systemctl', 'is-active', '--quiet', https.SERVICE.name)
    digest = hashlib.sha256(chain.read_bytes()).hexdigest()
    marker = state / 'loaded-server.sha256'
    if marker.exists() and https.protected(marker).read_text().strip() == digest:
        return
    pki.run(https.CADDY, 'validate', '--config', config, '--adapter', 'caddyfile')
    pki.run('systemctl', 'kill', '--kill-who=main', '--signal=SIGUSR1', https.SERVICE.name)
    expected = pki.run('openssl', 'x509', '-in', chain, '-outform', 'DER')
    context = ssl.create_default_context(cafile=str(root))
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    # TLS 1.3 exposes the server certificate before client authentication; no HTTP request is sent.
    for attempt in range(20):
        try:
            with socket.create_connection(('127.0.0.1', data['https_port']), timeout=2) as raw:
                with context.wrap_socket(raw, server_hostname=data['evi_name']) as tls:
                    if tls.getpeercert(binary_form=True) == expected:
                        pki.write(marker, (digest + '\n').encode(), backup=True)
                        print('Proxy serves the renewed server certificate; CSM was not restarted.')
                        return
        except (OSError, ssl.SSLError):
            pass
        time.sleep(0.2)
    raise ValueError('The proxy has not presented the new certificate. Check wattwell-csm-https.service; no charging service was restarted.')


def refresh_rpi(data, chain, key, root):
    runtime = https.protected(https.RUNTIME, directory=True)
    config = json.loads(https.protected(runtime / 'connection.json').read_text())
    if (config.get('host') != data['evi_name'] or config.get('port') != data['https_port']
            or config.get('address', '') != data['evi_ip']):
        raise ValueError('The runtime connection differs from the enrollment profile.')
    for source, name in ((key, 'client.key'), (root, 'root-ca.pem')):
        if https.protected(runtime / name).read_bytes() != source.read_bytes():
            raise ValueError('Runtime key/trust differs; use the HTTPS commissioning assistant for review.')
    target = https.protected(runtime / 'client-chain.pem')
    info = target.stat()
    if info.st_mode & 0o777 != 0o640:
        raise ValueError('Expected protected runtime certificate mode 0640.')
    if target.read_bytes() != chain.read_bytes():
        pki.write(target, chain.read_bytes(), 0o640, backup=True)
        os.chown(str(target), 0, info.st_gid)
        print('Client certificate refreshed; the updated backend loads it on its next request without restarting.')


def verified_renewal(data, role, chain, key, root, step):
    candidate_bytes = request_renewal(data, role, chain, key, root, step)
    with tempfile.TemporaryDirectory(prefix='.renew-', dir=str(chain.parent)) as temporary:
        candidate = Path(temporary) / 'chain.pem'
        candidate.write_bytes(candidate_bytes)
        pki.verify_certificate(candidate, key, root, role, data)
        end = expiry(candidate)
        start = pki.run('openssl', 'x509', '-in', candidate, '-startdate', '-noout').decode().strip().split('=', 1)[1]
        if end < expiry(chain) or end - ssl.cert_time_to_seconds(start) > 24 * 3600 + 600:
            raise ValueError('Renewed validity does not match the reviewed 24-hour policy.')
    return candidate_bytes


def renew(device, data, refresh_only=False):
    state = https.protected(pki.STATE / device, directory=True)
    with (state / 'renewal.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        directory, role, chain, key, root = identity(device, data)
        if not refresh_only and expiry(chain) - time.time() <= 8 * 3600:
            step = https.protected(INSTALL / 'step')
            candidate_bytes = verified_renewal(data, role, chain, key, root, step)
            pki.write(chain, candidate_bytes, 0o644, backup=True)
            print('Verified renewed certificate installed; the private key and CA trust are unchanged.')
        if device == 'evi':
            refresh_evi(data, chain, key, root, state)
        else:
            refresh_rpi(data, chain, key, root)


class HTTPSOnly(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        if not new_url.startswith('https://'):
            raise ValueError('Non-HTTPS download redirect refused.')
        return super().redirect_request(request, fp, code, message, headers, new_url)


def install_step():
    target = INSTALL / 'step'
    if target.exists() or target.is_symlink():
        https.protected(target)
        if not pki.run(target, 'version').startswith(('Smallstep CLI/' + VERSION + ' ').encode()):
            raise ValueError('An unexpected renewal binary exists; it was not overwritten.')
        return
    if platform.system() != 'Linux' or platform.machine() not in ARCHIVES:
        raise ValueError('The verified renewal binary supports Linux ARMv7, ARM64 and amd64 only.')
    arch, expected = ARCHIVES[platform.machine()]
    bundle = https.REPO / 'certs/cacert.pem'
    if bundle.exists():
        if hashlib.sha256(bundle.read_bytes()).hexdigest() != '796f1389bfea13c0ed78f663b765baaff54a508e5483786bd6ece73c5e3e3cbe':
            raise ValueError('The configurator CA bundle differs from its reviewed hash.')
    context = ssl.create_default_context(cafile=str(bundle) if bundle.exists() else None)
    opener = urllib.request.build_opener(HTTPSOnly(), urllib.request.HTTPSHandler(context=context))
    url = 'https://github.com/smallstep/cli/releases/download/v{0}/step_linux_{0}_{1}.tar.gz'.format(VERSION, arch)
    with tempfile.TemporaryDirectory(prefix='.step-', dir=str(INSTALL)) as temporary:
        archive, candidate = Path(temporary) / 'step.tar.gz', Path(temporary) / 'step'
        print('Downloading official Smallstep CLI ' + VERSION + ' for ' + arch + '; verifying SHA-256...', flush=True)
        with opener.open(url, timeout=30) as response, archive.open('wb') as output:
            size = 0
            while True:
                chunk = response.read(65536)
                if not chunk:
                    break
                size += len(chunk)
                if size > 50 * 1024 * 1024:
                    raise ValueError('Renewal tool download exceeds its size limit.')
                output.write(chunk)
        if hashlib.sha256(archive.read_bytes()).hexdigest() != expected:
            raise ValueError('Official renewal tool SHA-256 differs; nothing was executed.')
        with tarfile.open(str(archive), 'r:gz') as package:
            members = [m for m in package.getmembers() if m.name.endswith('/bin/step')]
            if len(members) != 1 or not members[0].isfile() or not 0 < members[0].size < 100 * 1024 * 1024:
                raise ValueError('Expected one regular renewal executable in the archive.')
            with package.extractfile(members[0]) as source, candidate.open('wb') as output:
                shutil.copyfileobj(source, output)
        candidate.chmod(0o755)
        print('Checking the verified executable version...', flush=True)
        if not pki.run(candidate, 'version').startswith(('Smallstep CLI/' + VERSION + ' ').encode()):
            raise ValueError('Unexpected renewal tool version.')
        os.link(str(candidate), str(target))


def backend_ready():
    https.backend_account()
    compiled = https.REPO / 'backend/dist/services/csm/RealCSMService.js'
    if not compiled.is_file() or 'refreshTLSCertificate' not in compiled.read_text():
        raise ValueError('Build the updated HMI backend first, then restart it once with charging stopped to load certificate hot reload.')


def install(device, data):
    directory, role, chain, key, root = identity(device, data)
    if device == 'rpi':
        backend_ready()
    if INSTALL.exists() or INSTALL.is_symlink():
        https.protected(INSTALL, directory=True)
        if not (INSTALL / 'managed').is_file() or (INSTALL / 'managed').read_text() != MARKER:
            raise ValueError('An unmanaged renewal directory was preserved.')
    for suffix in ('.service', '.timer'):
        path = UNITS / (NAME + suffix)
        if path.exists() or path.is_symlink():
            if not https.protected(path).read_text().startswith(MARKER):
                raise ValueError('An unmanaged renewal unit was preserved.')
    if not pki.yes('Install the verified renewal tool and an hourly local timer, without restarting charging services?'):
        return
    https.protected(INSTALL.parent, directory=True)
    INSTALL.mkdir(mode=0o700, parents=True, exist_ok=True)
    pki.write(INSTALL / 'managed', MARKER.encode())
    print('Preparing the private Smallstep renewal executable...', flush=True)
    install_step()
    print('Checking automatic certificate issuance with the Grid CA...', flush=True)
    # Exercise the exact CA authorization now; do not wait sixteen hours to discover a policy error.
    verified_renewal(data, role, chain, key, root, INSTALL / 'step')
    print('CA automatic issuance/renewal authorization verified; the preflight certificate was not installed.')
    print('Loading the verified certificate into the existing TLS transport...', flush=True)
    renew(device, data, refresh_only=True)
    (INSTALL / 'setup').mkdir(mode=0o700, exist_ok=True)
    for name in ('csm_renewal.py', 'csm_https.py', 'rpi_evi_certificates.py'):
        pki.write(INSTALL / 'setup' / name, (Path(__file__).parent / name).read_bytes(), backup=True)
    if device == 'evi':
        (INSTALL / 'config').mkdir(mode=0o700, exist_ok=True)
        name = 'config/csm-https.Caddyfile.example'
        pki.write(INSTALL / name, (https.REPO / name).read_bytes(), backup=True)
    (pki.STATE / device / 'step').mkdir(mode=0o700, exist_ok=True)
    write_paths = [str(pki.STATE / device), str(pki.DEVICE_DIRS[device])]
    if device == 'rpi':
        write_paths.append(str(https.RUNTIME))
    service = MARKER + '''[Unit]
Description=Renew the local CSM TLS certificate
After=network-online.target
Wants=network-online.target
[Service]
Type=oneshot
User=root
UMask=0077
TimeoutStartSec=90
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=strict
Environment=PYTHONDONTWRITEBYTECODE=1
''' + 'Environment=STEPPATH=' + str(pki.STATE / device / 'step') + '\n' + 'ExecStart=/usr/bin/python3 ' + str(INSTALL / 'setup/csm_renewal.py') + ' --run ' + device + '\n' + 'ReadWritePaths=' + ' '.join(write_paths) + '\n'
    timer = MARKER + '''[Unit]
Description=Check the CSM TLS certificate hourly
[Timer]
OnBootSec=2m
OnCalendar=hourly
RandomizedDelaySec=2m
Persistent=true
[Install]
WantedBy=timers.target
'''
    pki.write(UNITS / (NAME + '.service'), service.encode(), 0o644, backup=True)
    pki.write(UNITS / (NAME + '.timer'), timer.encode(), 0o644, backup=True)
    pki.run('systemctl', 'daemon-reload')
    pki.run('systemctl', 'enable', '--now', NAME + '.timer')
    print('Hourly renewal enabled; certificates renew with eight hours remaining. No charging service was restarted.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--apply', action='store_true', help='install the local renewal tool and timer')
    action.add_argument('--refresh', action='store_true', help='load an already reissued certificate without changing charging configuration')
    action.add_argument('--run', choices=('evi', 'rpi'), help=argparse.SUPPRESS)
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise ValueError('Run this local assistant with sudo.')
    os.umask(0o077)
    device = args.run or ('evi' if (https.REPO / 'scripts/prepare-csm-https.py').is_file() else 'rpi')
    data = pki.validate_profile(json.loads(https.protected(pki.STATE / device / 'profile.json').read_text()))
    print('Local CSM certificate renewal; role: ' + device + '. No charging service restart.')
    if args.apply:
        install(device, data)
    elif args.run or args.refresh:
        if args.refresh and device == 'rpi':
            backend_ready()
        renew(device, data, refresh_only=args.refresh)
    else:
        identity(device, data)
        print('Preview: --refresh loads an already reissued certificate; --apply installs the verified tool and hourly timer.')
        print('Expired certificates require interactive Grid reissuance. EVI automatic renewal needs its scoped public-key authorization on the Grid first.')


if __name__ == '__main__':
    try:
        main()
    except (EOFError, KeyboardInterrupt):
        raise SystemExit('Cancelled. No charging service was restarted.')
    except ValueError as error:
        raise SystemExit('ERROR: ' + str(error))
    except urllib.error.HTTPError as error:
        raise SystemExit('ERROR: Renewal tool download returned HTTP ' + str(error.code) + '. No certificate verification was bypassed.')
    except urllib.error.URLError as error:
        raise SystemExit('ERROR: Renewal tool download failed: ' + str(error.reason))
    except subprocess.CalledProcessError as error:
        raise SystemExit('ERROR: ' + Path(str(error.cmd[0])).name + ' exited with status ' + str(error.returncode)
                         + ' during the last printed step. Command arguments and private/token output suppressed.')
    except OSError as error:
        raise SystemExit('ERROR: ' + str(error))
    except Exception as error:
        raise SystemExit(type(error).__name__ + ': Renewal failed. Inspect certificate validity, CA connectivity and protected paths. Private/token output suppressed.')
