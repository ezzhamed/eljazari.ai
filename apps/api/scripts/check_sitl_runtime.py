from __future__ import annotations

import argparse
import json
import sys

from utils.sitl_runtime import require_real_sitl, resolve_sitl_runtime


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect the local PX4/Gazebo SITL runtime")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit non-zero unless a real native or WSL SITL runtime is available",
    )
    args = parser.parse_args()

    try:
        runtime = resolve_sitl_runtime()
    except RuntimeError as exc:
        payload = {
            "mode": "unavailable",
            "is_real": False,
            "required_real": require_real_sitl(),
            "diagnostics": [str(exc)],
        }
        print(json.dumps(payload, indent=2))
        print(f"[sitl] {exc}", file=sys.stderr)
        return 1

    payload = {
        "mode": runtime.mode,
        "is_real": runtime.is_real,
        "required_real": require_real_sitl(),
        "host_platform": runtime.host_platform,
        "px4_dir": runtime.px4_dir,
        "worlds_path": runtime.worlds_path,
        "wsl_distro": runtime.wsl_distro,
        "wsl_user": runtime.wsl_user,
        "wsl_python_bin": runtime.wsl_python_bin,
        "wsl_gz_bin": runtime.wsl_gz_bin,
        "diagnostics": list(runtime.diagnostics),
    }
    print(json.dumps(payload, indent=2))

    if args.strict and not runtime.is_real:
        print("[sitl] real SITL runtime is not available", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
