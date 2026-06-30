"""Docker image validation for the dashboard container."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = REPO_ROOT / "docker-compose.dashboard.yml"
DASHBOARD_SERVICE = "vulture-dashboard"


def _run(cmd: list[str], *, cwd: Path = REPO_ROOT) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker not available")
class TestDashboardDockerImage:
    def test_dashboard_image_imports_app(self):
        _run(
            [
                "docker",
                "compose",
                "-f",
                str(COMPOSE_FILE),
                "build",
                DASHBOARD_SERVICE,
            ]
        )
        image_id = _run(
            [
                "docker",
                "compose",
                "-f",
                str(COMPOSE_FILE),
                "images",
                "-q",
                DASHBOARD_SERVICE,
            ]
        ).stdout.strip()
        assert image_id, "dashboard image id missing after build"

        _run(["docker", "run", "--rm", image_id, "python", "-c", "import app"])

    def test_dashboard_container_health_endpoint(self):
        _run(
            [
                "docker",
                "compose",
                "-f",
                str(COMPOSE_FILE),
                "build",
                DASHBOARD_SERVICE,
            ]
        )
        _run(
            [
                "docker",
                "compose",
                "-f",
                str(COMPOSE_FILE),
                "up",
                "-d",
                "--force-recreate",
                "--no-deps",
                DASHBOARD_SERVICE,
            ]
        )
        try:
            health = subprocess.run(
                ["curl", "-fsS", "--max-time", "15", "http://localhost:8088/health"],
                capture_output=True,
                text=True,
            )
            assert health.returncode == 0, health.stderr or health.stdout
        finally:
            subprocess.run(
                [
                    "docker",
                    "compose",
                    "-f",
                    str(COMPOSE_FILE),
                    "rm",
                    "-sf",
                    DASHBOARD_SERVICE,
                ],
                check=False,
                capture_output=True,
                text=True,
            )
