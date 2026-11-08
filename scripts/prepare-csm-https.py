#!/usr/bin/python3
"""Prepare Caddy and an EVI server CSR, without changing or starting services."""
import argparse
import hashlib
import os
from pathlib import Path
import shutil
import stat
import sys
import tarfile
import tempfile

import configure

VERSION = '2.11.9'
ARCHIVE_SHA256 = '8c191dd2688c30dbe19793a0a3f532ede64547c5a70a47eebf7d8ab43a4d7514'
URL = 'https://github.com/caddyserver/caddy/releases/download/v{0}/caddy_{0}_linux_armv7.tar.gz'.format(VERSION)
TARGET = Path('/usr/local/bin/caddy')
DIRECTORY = Path('/etc/wattwell-evi/csm-https')


def install_caddy():
    if TARGET.is_symlink() or (TARGET.exists() and not TARGET.is_file()):
        raise RuntimeError('Caddy destination is not a regular file.')
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.caddy-install-', dir=str(TARGET.parent)) as temporary:
        directory = Path(temporary)
        archive = directory / 'caddy.tar.gz'
        print('Downloading official Caddy ' + VERSION + ' for ARMv7; verifying SHA-256...', flush=True)
        configure.download(configure.downloader(), URL, archive, ARCHIVE_SHA256)
        candidate = directory / 'caddy'
        with tarfile.open(str(archive), 'r:gz') as package:
            members = [member for member in package.getmembers() if member.name == 'caddy']
            if len(members) != 1 or not members[0].isfile() or not 0 < members[0].size <= 100 * 1024 * 1024:
                raise RuntimeError('Expected one regular Caddy binary in the archive.')
            with package.extractfile(members[0]) as source, candidate.open('wb') as output:
                shutil.copyfileobj(source, output)
        candidate.chmod(0o755)
        if not configure.run([candidate, 'version']).stdout.startswith('v' + VERSION + ' '):
            raise RuntimeError('Caddy did not report the expected version.')
        if TARGET.exists():
            if hashlib.sha256(TARGET.read_bytes()).digest() != hashlib.sha256(candidate.read_bytes()).digest():
                raise RuntimeError('A different Caddy already exists; it was not overwritten.')
            TARGET.chmod(0o755)
        else:
            os.link(str(candidate), str(TARGET))


def prepare_csr(hostname, openssl):
    if DIRECTORY.is_symlink() or (DIRECTORY.exists() and not DIRECTORY.is_dir()):
        raise RuntimeError('The TLS directory is not a regular directory.')
    DIRECTORY.mkdir(parents=True, mode=0o700, exist_ok=True)
    if DIRECTORY.stat().st_uid != os.geteuid():
        raise RuntimeError('The TLS directory must belong to the configuring user.')
    DIRECTORY.chmod(0o700)
    key, csr = DIRECTORY / 'server.key', DIRECTORY / 'server.csr'
    for path in (key, csr):
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise RuntimeError('TLS material must be a regular file: ' + str(path))
    if key.exists() != csr.exists():
        raise RuntimeError('Only part of the TLS identity exists; preserve it and review before retrying.')
    if not key.exists():
        # Generate privately first; never publish an incomplete key/CSR pair silently.
        with tempfile.TemporaryDirectory(prefix='.csr-', dir=str(DIRECTORY)) as temporary:
            temporary = Path(temporary)
            configure.run([openssl, 'req', '-new', '-newkey', 'ec', '-pkeyopt',
                           'ec_paramgen_curve:prime256v1', '-nodes', '-sha256',
                           '-keyout', temporary / 'key', '-out', temporary / 'csr',
                           '-subj', '/CN=' + hostname,
                           '-addext', 'subjectAltName=DNS:' + hostname,
                           '-addext', 'extendedKeyUsage=serverAuth'])
            (temporary / 'key').chmod(0o600)
            os.link(str(temporary / 'key'), str(key))
            os.link(str(temporary / 'csr'), str(csr))
    if key.stat().st_uid != os.geteuid() or stat.S_IMODE(key.stat().st_mode) != 0o600:
        raise RuntimeError('The private key must belong to the configuring user and have mode 0600.')
    configure.run([openssl, 'req', '-in', csr, '-verify', '-noout'])
    subject = configure.run([openssl, 'req', '-in', csr, '-noout', '-subject', '-nameopt', 'RFC2253']).stdout.strip()
    if subject != 'subject=CN=' + hostname:
        raise RuntimeError('The existing CSR belongs to another hostname; no key was replaced.')
    key_public = configure.run([openssl, 'pkey', '-in', key, '-passin', 'pass:', '-pubout']).stdout
    csr_public = configure.run([openssl, 'req', '-in', csr, '-pubkey', '-noout']).stdout
    if key_public != csr_public:
        raise RuntimeError('The existing CSR and private key do not match.')
    return csr


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--hostname', required=True, help='EVI DNS name, e.g. evi3.local; the CA must approve this name')
    parser.add_argument('--apply', action='store_true', help='install Caddy and generate/reuse the private key and public CSR')
    args = parser.parse_args()
    labels = args.hostname.split('.')
    if len(args.hostname) > 253 or len(labels) < 2 or not all(configure.valid_hostname(label) for label in labels):
        raise ValueError('Use a lowercase DNS hostname such as evi3.local.')
    if not args.apply:
        print('Would install verified Caddy ' + VERSION + ' and prepare a server CSR for ' + args.hostname)
        print('No packages, certificates, network settings or services were changed. Use --apply to prepare.')
        return
    if os.geteuid() != 0:
        raise RuntimeError('Preparation requires sudo.')
    if (configure.run(['dpkg', '--print-architecture']).stdout.strip() != 'armhf'
            or configure.run(['uname', '-m']).stdout.strip() != 'armv7l'):
        raise RuntimeError('This installer supports only ARMv7 armhf.')
    openssl = shutil.which('openssl')
    if not openssl:
        raise RuntimeError('OpenSSL is needed to generate the CSR; nothing was installed.')
    os.umask(0o077)
    install_caddy()
    csr = prepare_csr(args.hostname, openssl)
    print('Public CSR for signing by the existing Grid CA: ' + str(csr))
    print('The private key stays on this EVI. Transfer only server.csr, never server.key.')
    print('Preparation only: no proxy configuration was activated; CSM, OCPP, firewall and networking were not changed.')
    print('HTTPS/mTLS is not active. Follow docs/csm-https.md for the remaining commissioning steps.')


if __name__ == '__main__':
    try:
        main()
    except (Exception, KeyboardInterrupt) as error:
        print('ERROR: ' + str(error), file=sys.stderr)
        sys.exit(1)
