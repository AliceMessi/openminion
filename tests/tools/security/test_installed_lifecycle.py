"""Opt-in installed scanner proof, separate from ordinary hosted E2E selection."""

import json
import os
import shutil
from pathlib import Path

import pytest

from openminion.tools.security.plugin import (
    _h_scan_artifact,
    _h_scan_dependencies,
    _h_scan_secrets,
)
from openminion.tools.security.schemas import SecurityScanResult
from tests.e2e.project_worker.test_security_remediation import run_security_lifecycle
from tests.tools.security.test_audit_report import audit_context as audit_context

pytestmark = pytest.mark.e2e


def test_installed_semgrep_remediation_lifecycle(audit_context, tmp_path):
    if os.environ.get("OPENMINION_TEST_INSTALLED_SECURITY") != "1":
        pytest.skip(
            "set OPENMINION_TEST_INSTALLED_SECURITY=1 for installed scanner proof"
        )
    ctx, _ = audit_context
    evidence = run_security_lifecycle(ctx, tmp_path, installed=True)
    (tmp_path / "lifecycle-evidence.json").write_text(json.dumps(evidence, indent=2))


@pytest.fixture
def installed_trivy(monkeypatch):
    if os.environ.get("OPENMINION_TEST_INSTALLED_TRIVY") != "1":
        pytest.skip("set OPENMINION_TEST_INSTALLED_TRIVY=1 for installed Trivy proof")
    assert shutil.which("trivy"), "install Trivy before opting into this test"
    monkeypatch.setenv("TRIVY_OFFLINE_SCAN", "true")


@pytest.mark.parametrize(
    "handler,fixture,category",
    [
        (_h_scan_dependencies, "dependencies/requirements.txt", "dependency"),
        (_h_scan_artifact, "iac/main.tf", "misconfiguration"),
        (_h_scan_secrets, None, "secret"),
    ],
)
def test_installed_trivy_findings_and_canonical_evidence(
    installed_trivy, audit_context, tmp_path, handler, fixture, category
):
    ctx, _ = audit_context
    target = ctx.workspace / "scan-target"
    target.mkdir()
    synthetic_token = "ghp_" + "0123456789abcdefghijklmnopqrstuvwxyz"
    if fixture:
        source = Path(__file__).parent / "fixtures" / "targets" / fixture
        shutil.copyfile(source, target / source.name)
    else:
        (target / "synthetic.env").write_text(f"GITHUB_TOKEN={synthetic_token}\n")
    result = handler({"target": "scan-target", "include_evidence_artifact": True}, ctx)
    assert result["status"] == "completed", result.get("error")
    assert not result["truncated"] and result["total_findings"] > 0
    assert all(finding["category"] == category for finding in result["findings"])
    assert result["scanner_version"]
    ref = result["evidence_refs"][0]
    assert ref.startswith("artifact://sha256/")
    raw = ctx.artifactctl.read_bytes(ref)
    saved = SecurityScanResult.model_validate_json(raw)
    assert saved.findings and saved.status == "completed"
    assert synthetic_token not in raw.decode()
    assert synthetic_token not in json.dumps(result)
    (tmp_path / "installed-trivy-evidence.json").write_text(
        json.dumps(result, indent=2)
    )


def test_installed_trivy_missing_database_keeps_diagnostic(
    installed_trivy, audit_context, tmp_path, monkeypatch
):
    monkeypatch.setenv("TRIVY_CACHE_DIR", str(tmp_path / "empty-cache"))
    ctx, _ = audit_context
    result = _h_scan_dependencies({"target": "."}, ctx)
    assert not result["ok"] and result["status"] == "failed"
    assert result["error"]["code"] == "EXEC_ERROR"
    assert "--skip-db-update" in result["error"]["message"]
    assert not result["evidence_refs"]
