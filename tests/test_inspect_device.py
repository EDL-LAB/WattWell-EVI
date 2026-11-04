"""Run the POSIX script with fake system commands; no device access required."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "inspect-device.sh"
MOCK = r'''
import json
import os
from pathlib import Path
import sys

name = Path(sys.argv[0]).name
args = sys.argv[1:]
case = os.environ.get("INSPECTION_TEST_CASE", "healthy")
with open(os.environ["INSPECTION_TEST_TRACE"], "a") as trace:
    trace.write(json.dumps([name, args]) + "\n")

if name == "dpkg":
    if args == ["--print-architecture"]:
        print("armhf")
    elif args == ["--print-foreign-architectures"]:
        pass
    elif args == ["--version"]:
        print("Debian dpkg version 1.19.8 (armhf)")
    elif args == ["--get-selections"]:
        print("libc6\thold")
    elif args == ["--audit"]:
        if case == "audit-warning":
            print("package is unpacked but not configured")
        elif case == "audit-failed":
            print("cannot read database", file=sys.stderr)
            sys.exit(5)
    else:
        raise SystemExit("Unexpected dpkg invocation: " + repr(args))
elif name == "dpkg-query":
    assert args == ["-W", "-f=${Package}\\t${Architecture}\\t${Version}\\t${Status}\\n"]
    print("libc6\tarmhf\t2.28-10\thold ok installed")
    print("old-package\tarmhf\t1\tdeinstall ok config-files")
    if case == "half-configured":
        print("apt\tarmhf\t1.8.2.3\tinstall ok half-configured")
elif name == "cat":
    assert len(args) == 1
    content = {
        "/etc/os-release": 'ID=debian\nVERSION_ID="10"\nVERSION_CODENAME=buster',
        "/etc/debian_version": "10.13",
        "/usr/local/etc/distro_version": "evi-distro-v1.4.4",
        "/etc/hostname": "dhsom",
        "/etc/hosts": "127.0.0.1 localhost\n127.0.1.1 dhsom",
    }
    print(content[args[0]])
elif name == "systemctl":
    assert args[:2] == ["--no-pager", "--plain"]
    assert args[2:] in [
        ["list-units", "--all", "--type=service"],
        ["list-unit-files", "--type=service"],
        ["list-timers", "--all"],
        ["list-unit-files", "--type=timer"],
    ]
    if case == "systemctl-failed":
        print("Cannot connect to system bus", file=sys.stderr)
        sys.exit(1)
    print("evis-csm.service loaded active running")
elif name == "find":
    assert "-type" in args and ("-exec" in args or "-printf" in args)
    if case == "hash-failed":
        print("permission denied", file=sys.stderr)
        sys.exit(1)
elif name == "sha256sum":
    assert args[0] == "--"
    print("0" * 64 + "  " + args[1])
elif name == "ip":
    assert args in [["addr", "show"], ["-4", "route", "show", "table", "all"],
                    ["-6", "route", "show", "table", "all"]]
    if case == "ip-failed":
        print("Cannot read routes", file=sys.stderr)
        sys.exit(1)
    if args == ["addr", "show"]:
        print("inet 192.168.137.100/24 scope global eth1:0")
        print("inet 192.168.200.11/24 scope global eth1")
    else:
        print("default via 192.168.137.1 dev eth1")
elif name == "hostname":
    assert not args
    print("dhsom")
elif name == "ps":
    assert args == ["-eo", "pid,ppid,comm"]
    print("100 1 dhclient")
elif name == "date":
    print("2026-11-04T12:00:00Z")
elif name in ("uname", "id", "df", "findmnt"):
    print("test fixture")
else:
    raise SystemExit("Unexpected command: " + name)
'''


class InspectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="wattwell-inspect-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.trace = self.root / "trace.jsonl"
        self.report = self.root / "report with spaces"
        for command in ("mkdir", "awk", "sh"):
            os.symlink(shutil.which(command), self.bin / command)
        for command in ("dpkg", "dpkg-query", "cat", "systemctl", "find",
                        "sha256sum", "date", "uname", "id", "df", "findmnt",
                        "ip", "hostname", "ps"):
            path = self.bin / command
            path.write_text("#!" + sys.executable + "\n" + MOCK)
            path.chmod(0o700)

    def run_script(self, case="healthy", args=None):
        env = dict(os.environ, PATH=str(self.bin),
                   INSPECTION_TEST_TRACE=str(self.trace), INSPECTION_TEST_CASE=case)
        return subprocess.run(["/bin/sh", str(SCRIPT), *(args if args is not None
                              else [str(self.report)])], env=env, text=True,
                              capture_output=True, timeout=20)

    def test_complete_report_and_private_permissions(self):
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.report.stat().st_mode & 0o777, 0o700)
        for file in self.report.iterdir():
            self.assertEqual(file.stat().st_mode & 0o077, 0, file.name)
        self.assertIn("NOT authorized", (self.report / "summary.txt").read_text())
        self.assertIn("hold ok installed", (self.report / "packages.tsv").read_text())
        checks = (self.report / "checks.tsv").read_text().splitlines()[1:]
        self.assertTrue(checks)
        self.assertTrue(all(line.endswith("\t0") for line in checks), checks)
        commands = [json.loads(line)[0] for line in self.trace.read_text().splitlines()]
        self.assertNotIn("apt-get", commands)
        self.assertNotIn("ww_get_versions", commands)
        self.assertIn("192.168.137.100/24", (self.report / "ip-addresses.txt").read_text())
        self.assertIn("192.168.200.11/24", (self.report / "ip-addresses.txt").read_text())
        self.assertIn("dhclient: unavailable", (self.report / "available-tools.txt").read_text())
        self.assertEqual((self.report / "hostname.txt").read_text(), "dhsom\n")

    def test_existing_report_is_untouched(self):
        self.assertEqual(self.run_script().returncode, 0)
        before = {p.name: p.read_bytes() for p in self.report.iterdir()}
        self.assertNotEqual(self.run_script().returncode, 0)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.report.iterdir()})

    def test_existing_file_or_symlink_is_untouched(self):
        target = self.root / "target"
        target.write_text("preserve")
        self.assertNotEqual(self.run_script(args=[str(target)]).returncode, 0)
        self.report.symlink_to(self.root, target_is_directory=True)
        self.assertNotEqual(self.run_script().returncode, 0)
        self.assertEqual(target.read_text(), "preserve")
        self.assertFalse((self.root / "checks.tsv").exists())

    def test_missing_command_is_reported(self):
        (self.bin / "dpkg-query").unlink()
        result = self.run_script()
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("packages.tsv\t127", (self.report / "checks.tsv").read_text())
        self.assertIn("unavailable", (self.report / "packages.tsv.stderr").read_text())

    def test_incomplete_collection_and_package_states(self):
        for case in ("audit-warning", "audit-failed", "half-configured",
                     "systemctl-failed", "hash-failed", "ip-failed"):
            with self.subTest(case=case):
                self.report = self.root / case
                result = self.run_script(case)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("INCOMPLETE", (self.report / "summary.txt").read_text())

    def test_invalid_arguments_and_help_do_not_collect(self):
        for args in ([], ["relative"], [str(self.report), "extra"], ["--apply"]):
            with self.subTest(args=args):
                self.assertEqual(self.run_script(args=args).returncode, 1)
        self.assertEqual(self.run_script(args=["--help"]).returncode, 0)
        self.assertFalse(self.trace.exists())
        self.assertFalse(self.report.exists())


if __name__ == "__main__":
    unittest.main()
