#!/usr/bin/env python3
"""
eljazari.ai CLI Forwarder

Run this script from the workspace root to execute the command-line interface.
Usage:
  python cli.py --prompt "racing quadcopter" --auto-approve
"""

import sys
from pathlib import Path

# Add apps/api to Python path so absolute imports function correctly
root_dir = Path(__file__).parent.resolve()
api_dir = root_dir / "apps" / "api"

if str(api_dir) not in sys.path:
    sys.path.insert(0, str(api_dir))

from cli import main

if __name__ == "__main__":
    main()
