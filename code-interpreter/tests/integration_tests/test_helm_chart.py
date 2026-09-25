"""Render the Helm chart and check the executor settings. Skipped without helm."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, Final

import pytest
import yaml  # type: ignore[import-untyped]

from app.kubernetes_pod_config import parse_pod_resources

CHART: Final[Path] = Path(__file__).resolve().parents[3] / "kubernetes" / "code-interpreter"
NOTES_PROBE: Final[str] = 'notes: {{ include "code-interpreter.notes" . | toJson }}\n'
LEGACY_MEMORY_LIMIT: Final[str] = "codeInterpreter.kubernetesExecutor.podResources.limits.memory"

pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")


def _render(tmp_path: Path, *sets: str) -> tuple[dict[str, str], str]:
    """Return the service env and the rendered NOTES text."""
    chart = tmp_path / "chart"
    shutil.copytree(CHART, chart)
    (chart / "templates" / "notes-probe.yaml").write_text(NOTES_PROBE)
    args = ["helm", "template", "t", str(chart)]
    for value in sets:
        args += ["--set", value]
    rendered = subprocess.run(args, check=True, capture_output=True, text=True).stdout
    env: dict[str, str] = {}
    notes = ""
    for doc in yaml.safe_load_all(rendered):
        if not doc:
            continue
        if "notes" in doc:
            notes = doc["notes"]
        elif doc.get("kind") == "Deployment":
            container: dict[str, Any] = doc["spec"]["template"]["spec"]["containers"][0]
            env = {e["name"]: e["value"] for e in container["env"] if "value" in e}
    return env, notes


def test_default_executor_resources(tmp_path: Path) -> None:
    env, notes = _render(tmp_path)
    assert json.loads(env["KUBERNETES_EXECUTOR_POD_RESOURCES"]) == {
        "limits": {"cpu": "5"},
        "requests": {"cpu": "100m", "memory": "64Mi"},
    }
    assert "WARNING" not in notes


def test_null_resource_is_rendered_as_null(tmp_path: Path) -> None:
    env, _ = _render(tmp_path, "codeInterpreter.kubernetesExecutor.podResources.limits.cpu=null")
    raw = env["KUBERNETES_EXECUTOR_POD_RESOURCES"]
    assert json.loads(raw) == {
        "limits": {"cpu": None},
        "requests": {"cpu": "100m", "memory": "64Mi"},
    }
    assert parse_pod_resources("X", raw).limits == {"cpu": None}


def test_legacy_memory_limit_is_dropped_with_a_warning(tmp_path: Path) -> None:
    env, notes = _render(tmp_path, f"{LEGACY_MEMORY_LIMIT}=256Mi")
    resources = json.loads(env["KUBERNETES_EXECUTOR_POD_RESOURCES"])
    assert resources["limits"] == {"cpu": "5"}
    assert f"WARNING: {LEGACY_MEMORY_LIMIT} (256Mi) is ignored" in notes
    assert "memoryLimitMb" in notes
