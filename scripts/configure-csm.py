#!/usr/bin/python3
"""Preview or select a supplied CSM topology; Python 3.7+."""
import argparse
import fcntl
import hashlib
import ipaddress
import json
import os
import re
from pathlib import Path
import shutil
import sys
import tempfile

import configure

CONFIG_DIR = Path('/usr/local/etc/evis-csm')
STATE = configure.STATE / 'csm'
SERVICE_DROP_IN = Path('/etc/systemd/system/evis-csm.service.d/90-wattwell.conf')
SERVICE_COMMAND = '/usr/local/bin/evis-csm -c /usr/local/etc/evis-csm/evis-csm.toml'
TEMPLATE = configure.PROJECT / 'config/csm-1bmpu.toml'
# Only validated addresses, ports, the Modbus switch and graph path are substituted.
TEMPLATE_SHA256 = 'cdd59577f29b077a90f6d4882feba618bf3d27f5209b175b09954a1a52f0e3c5'

TOPOLOGIES = {
    '1bmpu': ('1 BMPU (node 96)', '0e3129062a82c98d292085fb95b72b37a972649b03004c8742cac15b3817864f'),
    '2bmpu-parallel': ('2 BMPUs in parallel (nodes 96, 97)', '675ecedde180b87292ceb6258e4ff3e2ca980fea08594067c33c36825c028661'),
    '2bmpu-series': ('2 BMPUs in series (nodes 96, 97)', 'd287430001b0c5829e1127a43c0c8f8131ebe2a232e357794fa9b3c00343476c'),
}


def graph_content(topology):
    data = (configure.PROJECT / ('config/csm-' + topology + '.dot')).read_bytes()
    if hashlib.sha256(data).hexdigest() != TOPOLOGIES[topology][1]:
        raise RuntimeError('The CSM graph differs from the reviewed example; stop for review.')
    return data


def choose_topology():
    if not sys.stdin.isatty():
        raise RuntimeError('Use --topology with 1bmpu, 2bmpu-parallel or 2bmpu-series when input is not interactive.')
    choices = list(TOPOLOGIES)
    for number, topology in enumerate(choices, 1):
        print('{}: {}'.format(number, TOPOLOGIES[topology][0]))
    while True:
        choice = input('CSM topology [1/2/3]: ').strip()
        if choice in ('1', '2', '3'):
            return choices[int(choice) - 1]
        print('Choose 1, 2 or 3.')


def port_number(value):
    if not str(value).isascii() or not str(value).isdecimal() or not 1 <= int(value) <= 65535:
        raise ValueError('Use a TCP port between 1 and 65535.')
    return int(value)


def evse_address(value):
    ip = ipaddress.IPv4Address(value)
    if ip.is_unspecified or ip.is_multicast or ip.is_loopback or int(ip) == 0xffffffff:
        raise ValueError('Use the unicast LAN IPv4 address of the EVSE/WDM.')
    return str(ip)


def modbus_address(value):
    if value == 'disabled':
        return value
    ip = ipaddress.IPv4Address(value)
    if ip.is_unspecified or ip.is_multicast or int(ip) == 0xffffffff:
        raise ValueError('Use disabled, 127.0.0.1 or a specific unicast IPv4 address for Modbus.')
    return str(ip)


def setting(value, prompt, default, validate):
    if value is not None or not sys.stdin.isatty():
        return validate(default if value is None else value)
    while True:
        try:
            return validate(input('{} [{}]: '.format(prompt, default)).strip() or default)
        except ValueError as error:
            print('ERROR: ' + str(error))


def profile(address, topology='1bmpu', http_port=8444, modbus_ip='disabled', modbus_port=5552):
    if topology not in TOPOLOGIES:
        raise ValueError('Unknown CSM topology: ' + topology)
    ip = evse_address(address)
    http_port = port_number(http_port)
    modbus_ip = modbus_address(modbus_ip)
    modbus_port = port_number(modbus_port)
    if modbus_ip != 'disabled' and http_port == modbus_port:
        raise ValueError('HTTP and enabled Modbus must use different ports.')
    template = TEMPLATE.read_bytes()
    if hashlib.sha256(template).hexdigest() != TEMPLATE_SHA256:
        raise RuntimeError('The CSM template differs from the reviewed profile; stop for review.')
    return template.decode().replace('@EVSE_IP@', ip).replace(
        '@HTTP_PORT@', str(http_port)).replace('@MODBUS_ENABLED@', str(modbus_ip != 'disabled').lower()).replace(
        '@MODBUS_IP@', '127.0.0.1' if modbus_ip == 'disabled' else modbus_ip).replace(
        '@MODBUS_PORT@', str(modbus_port)).replace(
        '/usr/local/etc/evis-csm/evis-csm-1bmpu.dot',
        '/usr/local/etc/evis-csm/evis-csm-wattwell-' + topology + '.dot')


def apply_profile(content, topology='1bmpu'):
    canonical = CONFIG_DIR / 'evis-csm.toml'
    managed = CONFIG_DIR / 'evis-csm-wattwell.toml'
    graph = CONFIG_DIR / ('evis-csm-wattwell-' + topology + '.dot')
    data = graph_content(topology)
    resolved = canonical.resolve(strict=True)
    if resolved.parent != CONFIG_DIR.resolve() or not resolved.is_file():
        raise RuntimeError('The existing CSM configuration must be a regular file inside ' + str(CONFIG_DIR))
    if graph.is_symlink() or (graph.exists() and not graph.is_file()):
        raise RuntimeError('The managed CSM graph is not a regular file: ' + str(graph))
    command = SERVICE_COMMAND + ' -g /usr/local/etc/evis-csm/' + graph.name
    override = '[Service]\nExecStart=\nExecStart=' + command + '\n'
    if SERVICE_DROP_IN.is_symlink() or (SERVICE_DROP_IN.exists() and not SERVICE_DROP_IN.is_file()):
        raise RuntimeError('The managed systemd override is not a regular file: ' + str(SERVICE_DROP_IN))
    result = configure.run(['systemctl', 'show', 'evis-csm.service', '--property=LoadState',
                            '--property=ActiveState', '--property=ExecStart'], check=False)
    status = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
    if result.returncode or status.get('LoadState') != 'loaded' or status.get('ActiveState') not in ('inactive', 'failed'):
        raise RuntimeError('CSM must already be stopped before applying this profile; no service was stopped automatically.')
    allowed = r'argv\[\]=' + re.escape(SERVICE_COMMAND) + r'(?: -g /usr/local/etc/evis-csm/[A-Za-z0-9_.-]+\.dot)? ;'
    if not re.search(allowed, status.get('ExecStart', '')):
        raise RuntimeError('The existing CSM ExecStart is not recognized; stop for review before replacing it.')
    unchanged = (canonical.read_text() == content and graph.exists() and graph.read_bytes() == data
                 and SERVICE_DROP_IN.exists() and SERVICE_DROP_IN.read_text() == override)
    if unchanged and 'argv[]=' + command + ' ;' in status['ExecStart']:
        print('The CSM configuration, graph and service command already match; nothing changed.')
        return
    if managed.is_symlink() or (managed.exists() and not managed.is_file()):
        raise RuntimeError('The managed CSM profile is not a regular file: ' + str(managed))
    if STATE.is_symlink():
        raise RuntimeError('The CSM state directory cannot be a symlink.')
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    STATE.chmod(0o700)
    with (STATE / 'configure.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        backup = Path(tempfile.mkdtemp(prefix='backup-', dir=str(STATE)))
        for source, name in ((resolved, 'evis-csm.toml'), (managed, 'managed-before.toml'),
                             (graph, 'graph-before.dot'), (SERVICE_DROP_IN, 'service-override-before.conf')):
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
        configure.replace_file(graph, data, backup=False)
        configure.replace_file(managed, content.encode(), backup=False)
        # The original vendor profile is kept intact, including when selected by a symlink.
        with tempfile.TemporaryDirectory(prefix='.csm-select-', dir=str(CONFIG_DIR)) as temporary:
            link = Path(temporary) / 'evis-csm.toml'
            link.symlink_to(managed.name)
            os.replace(str(link), str(canonical))
        configure.replace_file(SERVICE_DROP_IN, override.encode(), backup=False)
        configure.run(['systemctl', 'daemon-reload'])
        print('Selected CSM profile: ' + TOPOLOGIES[topology][0] + '; ' + str(canonical))
        print('Previous configuration and link information: ' + str(backup))
        print('CSM was not enabled, started or restarted. Service flags and vendor DOT graphs were not changed.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evse-ip', help='EVSE/WDM IPv4 address (default: 192.168.200.11); also used by the authorization endpoint')
    parser.add_argument('--http-port', type=port_number, help='loopback-only CSM HTTP port (default: 8444); match the proxy upstream')
    parser.add_argument('--modbus-ip', type=modbus_address, help='disabled (default), 127.0.0.1 or a specific LAN IPv4 to enable Modbus')
    parser.add_argument('--modbus-port', type=port_number, help='Modbus TCP port (default: 5552)')
    parser.add_argument('--topology', choices=TOPOLOGIES, help='skip the interactive topology question')
    parser.add_argument('--apply', action='store_true', help='write and select the profile; requires CSM already stopped')
    arguments = parser.parse_args()
    if arguments.apply and os.geteuid() != 0:
        raise RuntimeError('Apply with sudo sh configure-csm.sh --apply.')
    topology = arguments.topology or choose_topology()
    address = setting(arguments.evse_ip, 'Internal EVSE/WDM IPv4 (not the DHCP address)', '192.168.200.11', evse_address)
    print('CSM HTTP is loopback-only for the HTTPS proxy; this does not activate the proxy.')
    http_port = setting(arguments.http_port, 'Local CSM HTTP port; match the proxy upstream', 8444, port_number)
    print('LAN Modbus is a separate command path without this HTTPS/mTLS protection. Enable it only for a required integration.')
    modbus_ip = setting(arguments.modbus_ip, 'Modbus bind IPv4, or disabled', 'disabled', modbus_address)
    modbus_port = setting(arguments.modbus_port, 'Modbus TCP port', 5552, port_number) if modbus_ip != 'disabled' else (
        5552 if arguments.modbus_port is None else arguments.modbus_port)
    content = profile(address, topology, http_port, modbus_ip, modbus_port)
    graph_content(topology)
    print('Topology: ' + TOPOLOGIES[topology][0])
    print('CSM HTTP: 127.0.0.1:' + str(http_port) + '; EVSE/WDM: ' + address + ':8333')
    print('Modbus: ' + ('disabled' if modbus_ip == 'disabled' else modbus_ip + ':' + str(modbus_port)))
    print('Limits remain 9000 W / 490 V; validate wiring and protection before use.')
    print('Apply also aligns the systemd -g argument with the selected graph; no service is started.')
    if not arguments.apply:
        print(content, end='')
        print('\nPreview only. Use --apply to select this profile after checking the hardware and stopping CSM.')
        return
    os.umask(0o077)
    apply_profile(content, topology)


if __name__ == '__main__':
    try:
        main()
    except (Exception, KeyboardInterrupt) as error:
        print('ERROR: ' + str(error), file=sys.stderr)
        sys.exit(1)
