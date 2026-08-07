from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
DOCKER_SANDBOX_OVERLAY = REPO_ROOT / "docker" / "docker-compose.sandbox.docker.yml"


def _service_environment(service_name: str) -> dict[str, str]:
    compose = yaml.safe_load(DOCKER_SANDBOX_OVERLAY.read_text(encoding="utf-8"))
    entries = compose["services"][service_name]["environment"]
    return dict(entry.split("=", 1) for entry in entries)


def test_overlay_propagates_one_namespace_to_every_sandbox_capable_service():
    namespaces = {
        _service_environment(service)["XAGENT_SANDBOX_NAMESPACE"]
        for service in ("backend", "worker", "scheduler")
    }

    assert len(namespaces) == 1
