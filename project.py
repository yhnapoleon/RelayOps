"""CLI entry point for starting the RelayOps server."""

import argparse
import sys
from pathlib import Path

# When run as script (not installed), ensure src is on path so api, core, sdk, frontend are importable
if __name__ == "__main__":
    _root = Path(__file__).resolve().parent
    _src = _root / "src"
    if _src.is_dir() and str(_src) not in sys.path:
        sys.path.insert(0, str(_src))

import sdk as relayops

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Start the RelayOps server")
    parser.add_argument(
        "--host",
        type=str,
        default="0.0.0.0",
        help="Host to bind the server to (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port to bind the server to (default: 8000)",
    )
    args = parser.parse_args()

    relayops.serve(host=args.host, port=args.port)
