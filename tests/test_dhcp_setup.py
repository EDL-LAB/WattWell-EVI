"""Exercise service launch/lease hooks with fake tools and per-test state."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
MOCK = r'''
import json
import os
from pathlib import Path
import sys
name = Path(sys.argv[0]).name
case = os.environ['DHCP_CASE']
trace = Path(os.environ['DHCP_TRACE'])
if name == 'networkctl':
    print('State: routable (configured)' if case == 'managed' else 'State: routable (unmanaged)')
elif name == 'ps':
    print('dhclient /sbin/dhclient eth1' if case == 'existing' else 'sh sh start-dhcp.sh')
elif name == 'dhclient':
    trace.write_text(json.dumps(sys.argv[1:]))
elif name == 'vendor':
    sys.exit(7 if case == 'vendor-failed' else 0)
elif name == 'ip':
    if sys.argv[1:5] == ['-4', '-o', 'addr', 'show']:
        if case == 'present':
            print('5: eth1 inet 192.168.200.11/24 scope global eth1')
    elif sys.argv[1:4] == ['-4', 'addr', 'add']:
        with trace.open('a') as output:
            output.write(json.dumps(sys.argv[1:]) + '\n')
    else:
        raise SystemExit('Unexpected ip command')
'''


class DhcpSetupTests(unittest.TestCase):
    def fixture(self, root, name):
        binary = root / 'bin'
        binary.mkdir()
        for command in ('networkctl', 'ps', 'dhclient', 'vendor', 'ip'):
            path = binary / command
            path.write_text('#!' + sys.executable + '\n' + MOCK)
            path.chmod(0o700)
        content = (SCRIPTS / name).read_text().replace(
            'PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin',
            'PATH=' + str(binary) + ':/usr/bin:/bin')
        content = content.replace('/sbin/dhclient-script', str(binary / 'vendor'))
        content = content.replace('/var/lib/wattwell-evi/static-addresses', str(root / 'addresses'))
        script = root / name
        script.write_text(content)
        return script

    def test_existing_client_and_network_manager_prevent_duplicate_clients(self):
        for case in ('unmanaged', 'existing', 'managed'):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                script = self.fixture(root, 'start-dhcp.sh')
                trace = root / 'trace'
                result = subprocess.run(['/bin/sh', str(script), '--once'],
                    capture_output=True, text=True, timeout=10,
                    env=dict(os.environ, DHCP_CASE=case, DHCP_TRACE=str(trace)))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(trace.exists(), case == 'unmanaged')
                if trace.exists():
                    args = json.loads(trace.read_text())
                    self.assertEqual(args[-1], 'eth1')
                    self.assertIn('-1', args)
                    self.assertIn('-sf', args)

    def test_lease_hook_restores_missing_static_addresses_even_after_vendor_failure(self):
        for case, interface, expected_code in (('missing', 'eth1', 0),
                ('present', 'eth1', 0), ('vendor-failed', 'eth1', 7), ('missing', 'qca0', 1)):
            with self.subTest(case=case, interface=interface), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                script = self.fixture(root, 'dhclient-hook.sh')
                (root / 'addresses').write_text('192.168.200.11/24 eth1\n')
                trace = root / 'trace'
                result = subprocess.run(['/bin/sh', str(script)], capture_output=True,
                    text=True, timeout=10, env=dict(os.environ, DHCP_CASE=case,
                                                   DHCP_TRACE=str(trace), interface=interface))
                self.assertEqual(result.returncode, expected_code, result.stderr)
                self.assertEqual(trace.exists(), interface == 'eth1' and case != 'present')
                if trace.exists():
                    self.assertEqual(json.loads(trace.read_text()),
                        ['-4', 'addr', 'add', '192.168.200.11/24', 'dev', 'eth1', 'label', 'eth1'])


if __name__ == '__main__':
    unittest.main()
