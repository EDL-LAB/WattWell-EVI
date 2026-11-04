"""Test the trial launcher with fake commands, never touching host networking."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "request-dhcp.sh"
MOCK = r'''
import os
from pathlib import Path
import sys

name = Path(sys.argv[0]).name
case = os.environ.get("DHCP_TEST_CASE", "ok")
if name == "id":
    assert sys.argv[1:] == ["-u"]
    print("1000" if case == "unprivileged" else "0")
elif name == "networkctl":
    assert sys.argv[1:] == ["--no-pager", "status", "eth1"]
    if case == "state-failed":
        sys.exit(3)
    print("State: routable (configured)" if case == "managed" else "State: routable (unmanaged)")
elif name == "ps":
    assert sys.argv[1:] == ["-eo", "comm=,args="]
    if case == "ps-failed":
        sys.exit(3)
    if case in ("dhclient", "dhcpcd", "udhcpc"):
        print(case + " /sbin/" + case + " eth1")
    elif case == "loop":
        print("sh /bin/sh /usr/local/bin/dhcp-loop-eth1.sh")
    else:
        print("request-dhcp.sh sh request-dhcp.sh")
elif name == "dhclient":
    assert sys.argv[1:] == ["-4", "-v", "-1", "eth1"]
    Path(os.environ["DHCP_TEST_TRACE"]).write_text("requested eth1\n")
    sys.exit(2 if case == "no-lease" else 0)
'''


class DhcpTrialTests(unittest.TestCase):
    def test_preflight_and_client_exit_status(self):
        for case, expected in (("ok", 0), ("no-lease", 2), ("managed", 1),
                               ("unprivileged", 1), ("dhclient", 1),
                               ("dhcpcd", 1), ("udhcpc", 1), ("loop", 1),
                               ("ps-failed", 3), ("state-failed", 3)):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                for command in ("id", "networkctl", "ps", "dhclient"):
                    executable = root / command
                    executable.write_text("#!" + sys.executable + "\n" + MOCK)
                    executable.chmod(0o700)
                trace = root / "called"
                result = subprocess.run(
                    ["/bin/sh", str(SCRIPT)], text=True, capture_output=True,
                    env=dict(os.environ, PATH=str(root) + ":/usr/bin:/bin",
                             DHCP_TEST_CASE=case, DHCP_TEST_TRACE=str(trace)),
                    timeout=10,
                )
                self.assertEqual(result.returncode, expected, result.stderr)
                self.assertEqual(trace.exists(), case in ("ok", "no-lease"))


if __name__ == "__main__":
    unittest.main()
