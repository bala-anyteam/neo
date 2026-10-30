import os
import plistlib
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "retire_legacy_scheduler.py"


class RetireLegacySchedulerTests(unittest.TestCase):
    def test_disables_and_unloads_only_the_legacy_weekday_scheduler(self):
        self.assertTrue(SCRIPT.exists(), "legacy scheduler retirement command is missing")
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            agents = home / "Library" / "LaunchAgents"
            agents.mkdir(parents=True)
            legacy = agents / "com.bala.neo-bug-dms.plist"
            legacy.write_bytes(plistlib.dumps({
                "Label": "com.bala.neo-bug-dms",
                "ProgramArguments": ["python", "-m", "cos.bug_dms"],
                "StartCalendarInterval": {"Hour": 10, "Minute": 5},
            }))
            chakor = agents / "com.chakor.serve.plist"
            chakor_contents = plistlib.dumps({"Label": "com.chakor.serve"})
            chakor.write_bytes(chakor_contents)

            bin_dir = home / "bin"
            bin_dir.mkdir()
            calls = home / "launchctl.calls"
            launchctl = bin_dir / "launchctl"
            launchctl.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' \"$*\" >> \"$LAUNCHCTL_CALLS\"\n"
            )
            launchctl.chmod(0o755)
            env = os.environ | {
                "HOME": str(home),
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
                "LAUNCHCTL_CALLS": str(calls),
                "NEO_TEST_UID": "501",
            }

            result = subprocess.run(
                [str(SCRIPT)], env=env, text=True, capture_output=True, check=False
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(plistlib.loads(legacy.read_bytes())["Disabled"])
            self.assertEqual(chakor.read_bytes(), chakor_contents)
            self.assertEqual(
                calls.read_text().splitlines(),
                [
                    "disable gui/501/com.bala.neo-bug-dms",
                    "bootout gui/501/com.bala.neo-bug-dms",
                ],
            )


if __name__ == "__main__":
    unittest.main()
