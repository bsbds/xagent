#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from packaging.version import InvalidVersion, Version

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "src" / "xagent" / "_version.py"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Write the explicit Xagent package version module."
    )
    parser.add_argument("version", help="PEP 440 package version to write")
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="Version module path, defaults to src/xagent/_version.py",
    )
    return parser.parse_args()


def normalize_version(raw_version: str) -> str:
    try:
        return str(Version(raw_version))
    except InvalidVersion:
        raise ValueError(f"{raw_version!r} is not a valid PEP 440 version") from None


def write_version_file(version: str, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(f'__version__ = "{version}"\n')


def main() -> int:
    args = parse_args()
    try:
        version = normalize_version(args.version)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2

    write_version_file(version, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
