from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from packaging.version import Version

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "write_package_version.py"


def test_write_package_version_accepts_pep440_versions(tmp_path: Path) -> None:
    version_file = tmp_path / "_version.py"

    subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "1.2.3rc1",
            "--output",
            str(version_file),
        ],
        check=True,
        cwd=ROOT,
    )

    assert version_file.read_text() == '__version__ = "1.2.3rc1"\n'


def test_write_package_version_rejects_non_pep440_versions(tmp_path: Path) -> None:
    version_file = tmp_path / "_version.py"

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "nightly-20260526",
            "--output",
            str(version_file),
        ],
        cwd=ROOT,
        stderr=subprocess.PIPE,
        text=True,
    )

    assert result.returncode == 2
    assert "not a valid PEP 440 version" in result.stderr
    assert not version_file.exists()


def test_expected_ci_package_versions_are_pep440() -> None:
    expected_versions = [
        "1.2.3",
        "1.2.3rc1",
        "0.0.dev20260526",
        "0.0.0+abc123def456",
    ]

    assert [str(Version(version)) for version in expected_versions] == expected_versions
