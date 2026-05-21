from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read_workflow(name: str) -> str:
    return (ROOT / ".github" / "workflows" / name).read_text()


def read_repo_file(path: str) -> str:
    return (ROOT / path).read_text()


def test_nightly_build_uses_pep440_package_version() -> None:
    workflow = read_workflow("nightly-build.yml")

    assert 'NIGHTLY_VERSION="nightly-$NIGHTLY_DATE"' in workflow
    assert 'PACKAGE_VERSION="0.0.dev$NIGHTLY_DATE"' in workflow
    assert (
        "XAGENT_VERSION=${{ steps.version-meta.outputs.nightly_version }}" in workflow
    )
    assert (
        "XAGENT_PACKAGE_VERSION=${{ steps.version-meta.outputs.package_version }}"
        in workflow
    )
    assert (
        "XAGENT_PACKAGE_VERSION=${{ steps.version-meta.outputs.nightly_version }}"
        not in workflow
    )


def test_release_build_sanitizes_package_version_for_manual_runs() -> None:
    workflow = read_workflow("docker-publish.yml")

    assert 'PACKAGE_VERSION="${RELEASE_VERSION#v}"' in workflow
    assert 'PACKAGE_VERSION="0.0.0+${GITHUB_SHA::12}"' in workflow
    assert (
        "XAGENT_VERSION=${{ steps.version-meta.outputs.release_version }}" in workflow
    )
    assert (
        "XAGENT_PACKAGE_VERSION=${{ steps.version-meta.outputs.package_version }}"
        in workflow
    )
    assert (
        "XAGENT_PACKAGE_VERSION=${{ steps.version-meta.outputs.release_version }}"
        not in workflow
    )


def test_backend_dockerfile_applies_package_specific_vcs_version() -> None:
    dockerfile = read_repo_file("docker/Dockerfile.backend")

    assert (
        'SETUPTOOLS_SCM_PRETEND_VERSION_FOR_XAGENT="${XAGENT_PACKAGE_VERSION}"'
        in dockerfile
    )
    assert (
        'VCS_VERSIONING_PRETEND_VERSION_FOR_XAGENT="${XAGENT_PACKAGE_VERSION}"'
        in dockerfile
    )
    assert (
        'SETUPTOOLS_SCM_PRETEND_VERSION="${XAGENT_PACKAGE_VERSION}"' not in dockerfile
    )


def test_publish_script_sanitizes_v_prefixed_package_version() -> None:
    publish_script = read_repo_file("docker/publish.sh")

    assert (
        'PACKAGE_VERSION="${XAGENT_PACKAGE_VERSION:-${XAGENT_VERSION:-${TAG#v}}}"'
        in publish_script
    )
    assert '--build-arg "XAGENT_PACKAGE_VERSION=${PACKAGE_VERSION}"' in publish_script
    assert (
        '--build-arg "XAGENT_PACKAGE_VERSION=${XAGENT_PACKAGE_VERSION:-${XAGENT_VERSION:-${TAG}}}"'
        not in publish_script
    )


def test_backend_dockerfile_uses_frontend_managed_pptxgenjs() -> None:
    dockerfile = read_repo_file("docker/Dockerfile.backend")
    package_json = read_repo_file("frontend/package.json")

    assert '"pptxgenjs": "4.0.1"' in package_json
    assert "npm install -g pptxgenjs" not in dockerfile
    assert "/usr/lib/node_modules/pptxgenjs" not in dockerfile
    assert 'ENV NODE_PATH="/opt/xagent/frontend/node_modules"' in dockerfile
