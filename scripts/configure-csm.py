#!/usr/bin/python3
"""Preview or select the supplied single-BMPU CSM profile; Python 3.7+."""
import argparse
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

import configure

CONFIG_DIR = Path('/usr/local/etc/evis-csm')
STATE = configure.STATE / 'csm'
TEMPLATE = configure.PROJECT / 'config/csm-1bmpu.toml'
# The built-in template is validated in tests; only its IPv4 placeholder is variable.
TEMPLATE_SHA256 = '4cfd80d1d3b02797f19744d03cac4298cff1b7dd701f9d9a88d35ceefdb6bb99'


def profile(address):
    ip = ipaddress.IPv4Address(address)
    if ip.is_unspecified or ip.is_multicast or ip.is_loopback or int(ip) == 0xffffffff:
        raise ValueError('Use the unicast LAN IPv4 address of the EVSE/WDM.')
    template = TEMPLATE.read_bytes()
    if hashlib.sha256(template).hexdigest() != TEMPLATE_SHA256:
        raise RuntimeError('The CSM template differs from the reviewed profile; stop for review.')
    return template.decode().replace('@EVSE_IP@', str(ip))


def apply_profile(content):
    canonical = CONFIG_DIR / 'evis-csm.toml'
    managed = CONFIG_DIR / 'evis-csm-wattwell.toml'
    graph = CONFIG_DIR / 'evis-csm-1bmpu.dot'
    resolved = canonical.resolve(strict=True)
    if resolved.parent != CONFIG_DIR.resolve() or not resolved.is_file():
        raise RuntimeError('The existing CSM configuration must be a regular file inside ' + str(CONFIG_DIR))
    if not graph.is_file():
        raise RuntimeError('The existing single-BMPU graph is missing: ' + str(graph))
    if canonical.read_text() == content:
        print('The CSM configuration already matches; nothing changed.')
        return
    result = configure.run(['systemctl', 'show', 'evis-csm.service', '--property=LoadState',
                            '--property=ActiveState'], check=False)
    status = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
    if result.returncode or status.get('LoadState') != 'loaded' or status.get('ActiveState') not in ('inactive', 'failed'):
        raise RuntimeError('CSM must already be stopped before applying this profile; no service was stopped automatically.')
    if managed.is_symlink() or (managed.exists() and not managed.is_file()):
        raise RuntimeError('The managed CSM profile is not a regular file: ' + str(managed))
    if STATE.is_symlink():
        raise RuntimeError('The CSM state directory cannot be a symlink.')
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    STATE.chmod(0o700)
    with (STATE / 'configure.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        backup = Path(tempfile.mkdtemp(prefix='backup-', dir=str(STATE)))
        for source, name in ((resolved, 'evis-csm.toml'), (managed, 'managed-before.toml')):
            if source.is_file():
                saved = backup / name
                shutil.copy2(str(source), str(saved))
                info = source.stat()
                os.chown(str(saved), info.st_uid, info.st_gid)
        (backup / 'original.json').write_text(json.dumps({
            'canonical': str(canonical),
            'symlink': os.readlink(str(canonical)) if canonical.is_symlink() else None,
            'resolved': str(resolved),
        }, indent=2) + '\n')
        configure.replace_file(managed, content.encode(), backup=False)
        # The original vendor profile is kept intact, including when selected by a symlink.
        with tempfile.TemporaryDirectory(prefix='.csm-select-', dir=str(CONFIG_DIR)) as temporary:
            link = Path(temporary) / 'evis-csm.toml'
            link.symlink_to(managed.name)
            os.replace(str(link), str(canonical))
        print('Selected single-BMPU CSM profile: ' + str(canonical))
        print('Previous configuration and link information: ' + str(backup))
        print('CSM was not enabled, started or restarted. Service flags and the DOT graph were not changed.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evse-ip', default='192.168.200.11', help='EVSE/WDM IPv4 address; also used by the authorization endpoint')
    parser.add_argument('--apply', action='store_true', help='write and select the profile; requires CSM already stopped')
    arguments = parser.parse_args()
    content = profile(arguments.evse_ip)
    if not arguments.apply:
        print(content, end='')
        print('\nPreview only. Use --apply to select this profile after checking the hardware and stopping CSM.')
        return
    if os.geteuid() != 0:
        raise RuntimeError('Apply with sudo sh configure-csm.sh --apply.')
    os.umask(0o077)
    apply_profile(content)


if __name__ == '__main__':
    try:
        main()
    except (Exception, KeyboardInterrupt) as error:
        print('ERROR: ' + str(error), file=sys.stderr)
        sys.exit(1)
