#!/usr/bin/env python3
"""Permanently retire Neo's superseded weekday bug-DM LaunchAgent."""

import os
import plistlib
import subprocess
from pathlib import Path


LABEL = "com.bala.neo-bug-dms"


def main() -> None:
    home = Path.home()
    plist = home / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    if plist.exists():
        job = plistlib.loads(plist.read_bytes())
        job["Disabled"] = True
        replacement = plist.with_suffix(".plist.tmp")
        replacement.write_bytes(plistlib.dumps(job))
        replacement.replace(plist)

    uid = os.environ.get("NEO_TEST_UID", str(os.getuid()))
    target = f"gui/{uid}/{LABEL}"
    subprocess.run(["launchctl", "disable", target], check=False)
    subprocess.run(["launchctl", "bootout", target], check=False)


if __name__ == "__main__":
    main()
