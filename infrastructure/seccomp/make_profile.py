#!/usr/bin/env python3
"""Derive the simulation container's seccomp profile from Docker's default.

Docker's default profile (moby v28.3.2, ``moby-default-v28.3.2.json``, verbatim,
Apache-2.0) allows ``unshare``, ``mount`` and ``umount2`` only to containers
holding CAP_SYS_ADMIN, and ``pivot_root`` not at all. The mission sandbox
(apps/api/utils/mission_sandbox.py) needs exactly those four to build its
unprivileged user/mount/network/PID namespaces inside the container. This script
adds one rule allowing them without the capability and changes nothing else;
the container still runs without CAP_SYS_ADMIN.

    python infrastructure/seccomp/make_profile.py            # write sim-sandbox.json
    python infrastructure/seccomp/make_profile.py --check    # fail if it would change
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BASE = HERE / "moby-default-v28.3.2.json"
BASE_SHA256 = "01536f1d1df938ae611eba20d6349e0de7a99b6ecdee1549427a0b01b8301e28"
OUT = HERE / "sim-sandbox.json"
SANDBOX_SYSCALLS = ["mount", "pivot_root", "umount2", "unshare"]


def build() -> bytes:
    raw = BASE.read_bytes()
    if hashlib.sha256(raw).hexdigest() != BASE_SHA256:
        raise SystemExit(f"{BASE.name} is not the verbatim moby v28.3.2 profile")
    profile = json.loads(raw)
    profile["syscalls"].append(
        {
            "names": SANDBOX_SYSCALLS,
            "action": "SCMP_ACT_ALLOW",
            "comment": (
                "eljazari mission sandbox: unprivileged user namespaces, private "
                "mounts and pivot_root without CAP_SYS_ADMIN"
            ),
        }
    )
    return (json.dumps(profile, indent=2) + "\n").encode("utf-8")


def main(argv: list[str]) -> int:
    data = build()
    if "--check" in argv:
        current = OUT.read_bytes() if OUT.exists() else b""
        if current != data:
            print(f"{OUT.name} is out of date; run {Path(__file__).name}")
            return 1
        print(f"{OUT.name} matches its base")
        return 0
    OUT.write_bytes(data)
    print(f"wrote {OUT.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
