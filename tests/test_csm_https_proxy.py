"""Optional real TLS test: CADDY_TEST_BINARY=/path/to/pinned/caddy python -m unittest ..."""
import http.client
import os
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

CADDY = os.environ.get('CADDY_TEST_BINARY')
EXAMPLE = Path(__file__).resolve().parents[1] / 'config/csm-https.Caddyfile.example'


@unittest.skipUnless(CADDY and shutil.which('openssl'), 'Set CADDY_TEST_BINARY to run isolated real mTLS checks')
class ProxyTests(unittest.TestCase):
    def test_verified_client_only_can_reach_restricted_loopback_upstream(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            def openssl(*args):
                subprocess.run(['openssl'] + [str(arg) for arg in args], check=True,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            def authority(name):
                openssl('req', '-x509', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:prime256v1',
                        '-nodes', '-keyout', root / (name + '.key'), '-out', root / (name + '.pem'),
                        '-days', '1', '-subj', '/CN=' + name, '-addext', 'basicConstraints=critical,CA:TRUE')
            def leaf(name, cn, ca, purpose, san):
                openssl('req', '-new', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:prime256v1',
                        '-nodes', '-keyout', root / (name + '.key'), '-out', root / (name + '.csr'), '-subj', '/CN=' + cn)
                extensions = root / (name + '.ext')
                extensions.write_text('basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\n'
                                      'extendedKeyUsage=' + purpose + '\nsubjectAltName=DNS:' + san + '\n')
                openssl('x509', '-req', '-in', root / (name + '.csr'), '-CA', root / (ca + '.pem'),
                        '-CAkey', root / (ca + '.key'), '-CAcreateserial', '-days', '1',
                        '-extfile', extensions, '-out', root / (name + '.pem'))
            authority('ca')
            authority('foreign')
            leaf('server', 'evi3.local', 'ca', 'serverAuth', 'evi3.local')
            leaf('client', 'evcharger-csm-client', 'ca', 'clientAuth', 'evcharger-csm-client.local')
            leaf('grid', 'grid-evi-telemetry', 'ca', 'clientAuth', 'grid-evi-telemetry.local')
            leaf('other', 'another-client', 'ca', 'clientAuth', 'another-client.local')
            leaf('foreign-client', 'evcharger-csm-client', 'foreign', 'clientAuth', 'evcharger-csm-client.local')
            calls = []
            class Upstream(BaseHTTPRequestHandler):
                def do_GET(self):
                    calls.append((self.command, self.path))
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b'{"test_upstream":true}')
                do_PUT = do_GET
                def log_message(self, *args):
                    pass
            upstream = HTTPServer(('127.0.0.1', 0), Upstream)
            worker = threading.Thread(target=upstream.serve_forever, daemon=True)
            worker.start()
            with socket.socket() as probe:
                probe.bind(('127.0.0.1', 0))
                port = probe.getsockname()[1]
            config = EXAMPLE.read_text().replace('https://evi3.local:8443 {',
                'https://evi3.local:' + str(port) + ' {\n    bind 127.0.0.1')
            config = config.replace('/etc/wattwell-evi/csm-https/server-chain.pem', str(root / 'server.pem'))
            config = config.replace('/etc/wattwell-evi/csm-https/server.key', str(root / 'server.key'))
            config = config.replace('/etc/wattwell-evi/csm-https/client-ca.pem', str(root / 'ca.pem'))
            config = config.replace('127.0.0.1:8444', '127.0.0.1:' + str(upstream.server_port))
            path = root / 'Caddyfile'
            path.write_text(config)
            environment = dict(os.environ, XDG_DATA_HOME=str(root / 'data'), XDG_CONFIG_HOME=str(root / 'config'))
            subprocess.run([CADDY, 'validate', '--config', str(path), '--adapter', 'caddyfile'], check=True,
                           env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            process = subprocess.Popen([CADDY, 'run', '--config', str(path), '--adapter', 'caddyfile'],
                                       env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                deadline = time.monotonic() + 10
                while True:
                    try:
                        with socket.create_connection(('127.0.0.1', port), timeout=0.2):
                            break
                    except OSError:
                        if process.poll() is not None or time.monotonic() > deadline:
                            self.fail('Test proxy did not start')
                        time.sleep(0.05)
                def request(client, target='/api/v1/csm/status', method='GET', hostname='evi3.local', ca='ca'):
                    context = ssl.create_default_context(cafile=str(root / (ca + '.pem')))
                    if client:
                        context.load_cert_chain(str(root / (client + '.pem')), str(root / (client + '.key')))
                    with socket.create_connection(('127.0.0.1', port), timeout=3) as tcp:
                        with context.wrap_socket(tcp, server_hostname=hostname) as secured:
                            connection = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
                            connection.sock = secured
                            connection.request(method, target, headers={'Host': 'evi3.local',
                                               'X-SSL-Client-Verify': 'SUCCESS',
                                               'X-SSL-Client-S-DN': 'CN=evcharger-csm-client'})
                            response = connection.getresponse()
                            body = response.read()
                            return response.status, body
                self.assertEqual(request('client')[0], 200)
                self.assertEqual(request('client', '/api/v1/evse/1/authorize?authorize=true&idToken=test', 'PUT')[0], 200)
                for evse in (1, 2):
                    self.assertEqual(request('grid', f'/api/v1/evse/{evse}/data')[0], 200)
                for node_id in range(94, 110):
                    self.assertEqual(request('grid', f'/api/v1/pu/{node_id}/data')[0], 200)
                accepted = len(calls)
                for method, path in (
                    ('PUT', '/api/v1/evse/1/data'), ('GET', '/api/v1/evse/3/data'),
                    ('GET', '/api/v1/evses/data'), ('GET', '/api/v1/csm/status'),
                    ('GET', '/api/v1/evse/1/authorize'), ('PUT', '/api/v1/evse/1/power'),
                    ('GET', '/api/v1/pu/93/data'), ('GET', '/api/v1/pu/110/data'),
                    ('GET', '/api/v1/pu/data'), ('PUT', '/api/v1/pu/96/data'),
                    ('GET', '/api/v1/pu/96/data/extra'),
                ):
                    with self.subTest(method=method, path=path):
                        self.assertEqual(request('grid', path, method)[0], 403)
                self.assertEqual(request('other')[0], 403)
                self.assertEqual(request('other', '/api/v1/pu/96/data')[0], 403)
                self.assertEqual(request('client', '/api/v1/pu/96/data')[0], 403)
                self.assertEqual(request('client', '/api/v1/csm/restart')[0], 403)
                self.assertEqual(request('client', method='POST')[0], 403)
                for client in (None, 'foreign-client'):
                    with self.subTest(client=client), self.assertRaises((ssl.SSLError, ConnectionError)):
                        request(client)
                with self.assertRaises(ssl.SSLError):
                    request('client', hostname='wrong.local')
                with self.assertRaises(ssl.SSLCertVerificationError):
                    request('client', ca='foreign')
                self.assertEqual(len(calls), accepted, 'Denied requests must not reach the CSM upstream')
            finally:
                process.terminate()
                process.wait(timeout=5)
                upstream.shutdown()
                upstream.server_close()
                worker.join(timeout=3)


if __name__ == '__main__':
    unittest.main()
