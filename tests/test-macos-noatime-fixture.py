#!/usr/bin/env python3
"""Mocked fixture control-flow checks; NOT evidence of real macOS/noatime.

The hosted macOS job must separately verify mount flags and real read timestamps.
No disk image or mount is created by these tests, on any operating system.
"""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class NoatimeFixtureControlTests(unittest.TestCase):
    def run_fixture(self, failure=""):
        temporary = tempfile.TemporaryDirectory(prefix="noatime-control-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        area = root / "fixture-area"
        area.mkdir()
        commands = root / "commands"
        commands.mkdir()
        log = root / "calls"
        tools = {
            "uname": 'echo Darwin\n',
            "mktemp": 'echo "$MOCK_AREA"\n',
            "hdiutil": '''printf 'hdiutil %s\\n' "$*" >> "$MOCK_LOG"
if [[ "$1" == detach && "$MOCK_FAILURE" == detach ]]; then exit 1; fi
''',
            "sudo": '''printf 'sudo %s\\n' "$*" >> "$MOCK_LOG"
[[ "$MOCK_FAILURE" != remount ]]
''',
            "mount": '''if [[ "$MOCK_FAILURE" == flags ]]; then
  echo "/dev/disk999s1 on $MOCK_AREA/volume (apfs, local)"
else
  echo "/dev/disk999s1 on $MOCK_AREA/volume (apfs, local, noatime)"
fi
''',
            "python3": '''printf 'python3 %s\\n' "$*" >> "$MOCK_LOG"
if [[ "$1" == - ]]; then
  cat >/dev/null
  [[ "$MOCK_FAILURE" != probe ]]
else
  [[ "$MOCK_FAILURE" != suite ]]
fi
''',
        }
        for name, body in tools.items():
            path = commands / name
            path.write_text("#!/usr/bin/env bash\nset -eu\n" + body)
            path.chmod(0o755)
        env = dict(os.environ, PATH=f"{commands}:{os.environ['PATH']}",
                   MOCK_AREA=str(area), MOCK_LOG=str(log), MOCK_FAILURE=failure)
        result = subprocess.run(["bash", str(ROOT / "tests/run-macos-noatime.sh")],
                                env=env, capture_output=True, text=True)
        return result, area, log.read_text() if log.exists() else ""

    def test_supported_commands_and_successful_cleanup(self):
        result, area, log = self.run_fixture()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("-mountOptions", log)
        self.assertIn("-owners on -mountpoint", log)
        self.assertIn("sudo -n /sbin/mount -u -o noatime", log)
        self.assertIn("python3 -\n", log)
        self.assertIn("test-installer-atime-contract.py -v", log)
        self.assertIn("hdiutil detach", log)
        self.assertFalse(area.exists())

    def test_remount_failure_is_unverified_not_passed(self):
        result, area, log = self.run_fixture("remount")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("UNVERIFIED", result.stderr)
        self.assertNotIn("python3", log)
        self.assertIn("hdiutil detach", log)
        self.assertFalse(area.exists())

    def test_missing_mount_flag_does_not_run_probe_or_suites(self):
        result, area, log = self.run_fixture("flags")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("UNVERIFIED", result.stderr)
        self.assertNotIn("python3", log)
        self.assertFalse(area.exists())

    def test_failed_real_read_probe_does_not_run_suites(self):
        result, area, log = self.run_fixture("probe")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("python3 -\n", log)
        self.assertNotIn("test-installer-atime-contract.py", log)
        self.assertFalse(area.exists())

    def test_suite_failure_is_not_hidden(self):
        result, area, log = self.run_fixture("suite")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("test-installer-atime-contract.py", log)
        self.assertIn("test-macos-metadata-contract.py", log)
        self.assertFalse(area.exists())

    def test_detach_failure_retains_fixture(self):
        result, area, log = self.run_fixture("detach")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("did not detach", result.stderr)
        self.assertIn("hdiutil detach", log)
        self.assertTrue(area.exists())
        self.assertTrue((area / "volume/tmp").is_dir())


if __name__ == "__main__":
    unittest.main(verbosity=2)
