"""One MAVLink link for MAVSDK: the patch shared by the WSL and Docker simulators.

PX4's stock ``px4-rc.mavlink`` already runs an ``onboard`` link to MAVSDK's
port (14540). Two mistakes shipped before and must not come back:

* A second ``-m custom`` link aimed at the same port: two channels answered one
  client and the sequenced mission upload collapsed with PROTOCOL_ERROR.
* Moving the stock link off 14540 left only the custom link, whose stream set
  starts empty (no SYS_STATUS, GPS_RAW_INT, HOME_POSITION), so MAVSDK never saw
  sensor health and every mission died in preflight.

So the stock link stays, gets higher telemetry rates, and is addressed with
``-t`` when MAVSDK runs on another host or container. Idempotent. Standard
library only: it runs under WSL's system Python and inside the sim image.

    python3 px4_mavlink_patch.py --source ROMFS/.../px4-rc.mavlink \\
        --target rootfs/etc/init.d-posix/px4-rc.mavlink [--sdk-host HOST] [--git-repo PX4]
"""

from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path

MARKER = "Higher rates for MAVSDK clients"
_RC_PATH = "ROMFS/px4fmu_common/init.d-posix/px4-rc.mavlink"
_OFFBOARD_START = re.compile(r"^(mavlink start )(.*\$udp_offboard_port_local.*)$", re.MULTILINE)
_STREAMS = (
    f"\n# {MARKER} (eljazari)\n"
    "mavlink stream -r 50 -s LOCAL_POSITION_NED -u $udp_offboard_port_local\n"
    "mavlink stream -r 50 -s GLOBAL_POSITION_INT -u $udp_offboard_port_local\n"
    "mavlink stream -r 50 -s ATTITUDE -u $udp_offboard_port_local\n"
    "mavlink stream -r 10 -s BATTERY_STATUS -u $udp_offboard_port_local\n"
)


def patch(text: str, sdk_host: str = "127.0.0.1") -> str:
    """Return ``text`` with the stock offboard link tuned (and addressed) for MAVSDK."""
    start = _OFFBOARD_START.search(text)
    if start is None:
        raise ValueError("Could not find the offboard mavlink start line")
    if MARKER not in text:
        text = text[: start.end()] + _STREAMS + text[start.end() :]
    # Skipped for localhost, where PX4's default is already right and -t only narrows it.
    # A file patched in place (Docker restarts) may carry an older host: replace it.
    start = _OFFBOARD_START.search(text)
    assert start is not None
    arguments = re.sub(r"^-t \S+ ", "", start.group(2))
    if sdk_host not in ("", "127.0.0.1", "localhost"):
        arguments = f"-t {sdk_host} {arguments}"
    return text[: start.start()] + start.group(1) + arguments + text[start.end() :]


def _pristine(source: Path, git_repo: str | None) -> str:
    """The committed file when available, so repeated runs never stack edits."""
    if git_repo:
        shown = subprocess.run(
            ["git", "-C", git_repo, "show", f"HEAD:{_RC_PATH}"],
            capture_output=True,
            text=True,
            check=False,
        )
        if shown.returncode == 0 and shown.stdout.strip():
            return shown.stdout
    return source.read_text(encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--target", required=True, type=Path)
    parser.add_argument("--sdk-host", default="127.0.0.1")
    parser.add_argument("--git-repo", default=None)
    args = parser.parse_args(argv)
    patched = patch(_pristine(args.source, args.git_repo), args.sdk_host.strip())
    args.target.write_text(patched, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
