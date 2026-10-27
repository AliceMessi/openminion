from pathlib import Path
import json

import pytest

from openminion.tools.security.config import SecurityConfig
from openminion.tools.security.process import ScannerProcessResult
from openminion.tools.security.providers import trivy
from openminion.tools.security.schemas import LocalScanArgs

FIXTURES = Path(__file__).parent / "fixtures" / "trivy"


def _config(tmp_path: Path) -> SecurityConfig:
    return SecurityConfig(
        workspace_root=tmp_path,
        allowed_roots=(tmp_path,),
        semgrep_executable="semgrep",
        semgrep_config="rules.yml",
        trivy_executable="trivy",
    )


def _runner(payload: str, *, return_code: int = 1, stderr: str = ""):
    def run(argv, *, cwd, timeout_seconds):
        del cwd, timeout_seconds
        if "--version" in argv:
            return ScannerProcessResult(return_code=0, stdout="Version: 0.70.0\n")
        assert "--skip-db-update" in argv
        assert argv[-1]
        return ScannerProcessResult(
            return_code=return_code, stdout=payload, stderr=stderr
        )

    return run


@pytest.mark.parametrize(
    ("scanner", "args", "fixture", "category", "severity"),
    [
        (
            trivy.scan_dependencies,
            LocalScanArgs(target="."),
            "vulnerability.json",
            "dependency",
            "high",
        ),
        (
            trivy.scan_artifact,
            LocalScanArgs(target="."),
            "misconfiguration.json",
            "misconfiguration",
            "critical",
        ),
        (
            trivy.scan_secrets,
            LocalScanArgs(target="."),
            "secret.json",
            "secret",
            "high",
        ),
    ],
)
def test_trivy_normalizes_supported_local_scans(
    monkeypatch,
    tmp_path: Path,
    scanner,
    args,
    fixture: str,
    category: str,
    severity: str,
) -> None:
    monkeypatch.setattr(trivy.shutil, "which", lambda _name: "/usr/bin/trivy")
    result = scanner(
        args,
        target=tmp_path,
        config=_config(tmp_path),
        process_runner=_runner((FIXTURES / fixture).read_text()),
    )
    assert result.status == "completed"
    assert result.total_findings == 1
    assert result.configuration_identity is not None
    assert result.configuration_identity.scanner_version == "Version: 0.70.0"
    assert "--skip-db-update" in result.configuration_identity.fixed_flags
    assert result.findings[0].category == category
    assert result.findings[0].normalized_severity == severity
    assert "SYNTHETIC_SECRET_VALUE" not in repr(result)


def test_trivy_clean_unavailable_malformed_timeout_and_partial(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(trivy.shutil, "which", lambda _name: None)
    unavailable = trivy.scan_dependencies(
        LocalScanArgs(target="."), target=tmp_path, config=_config(tmp_path)
    )
    assert unavailable.status == "unavailable"
    assert unavailable.configuration_identity is None

    monkeypatch.setattr(trivy.shutil, "which", lambda _name: "/usr/bin/trivy")
    clean = trivy.scan_dependencies(
        LocalScanArgs(target="."),
        target=tmp_path,
        config=_config(tmp_path),
        process_runner=_runner((FIXTURES / "clean.json").read_text(), return_code=0),
    )
    assert clean.status == "completed"
    assert clean.total_findings == 0

    malformed = trivy.scan_dependencies(
        LocalScanArgs(target="."),
        target=tmp_path,
        config=_config(tmp_path),
        process_runner=_runner((FIXTURES / "malformed.json").read_text()),
    )
    assert malformed.status == "failed"
    assert malformed.error and malformed.error.code == "INVALID_RESPONSE"

    def timeout_runner(argv, *, cwd, timeout_seconds):
        del cwd, timeout_seconds
        if "--version" in argv:
            return ScannerProcessResult(return_code=0, stdout="Version: 0.70.0")
        return ScannerProcessResult(return_code=124, timed_out=True)

    timed_out = trivy.scan_dependencies(
        LocalScanArgs(target="."),
        target=tmp_path,
        config=_config(tmp_path),
        process_runner=timeout_runner,
    )
    assert timed_out.status == "timed_out"

    partial = trivy.scan_dependencies(
        LocalScanArgs(target="."),
        target=tmp_path,
        config=_config(tmp_path),
        process_runner=_runner(
            (FIXTURES / "vulnerability.json").read_text(), return_code=2
        ),
    )
    assert partial.status == "partial"
    assert partial.total_findings == 1


def test_trivy_version_failure_keeps_fixed_configuration_identity(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(trivy.shutil, "which", lambda _name: "/usr/bin/trivy")

    def version_failure(argv, *, cwd, timeout_seconds):
        del argv, cwd, timeout_seconds
        return ScannerProcessResult(return_code=1, stderr="version failed")

    result = trivy.scan_dependencies(
        LocalScanArgs(target="."),
        target=tmp_path,
        config=_config(tmp_path),
        process_runner=version_failure,
    )

    assert result.status == "unavailable"
    assert result.configuration_identity is not None
    assert result.configuration_identity.scanner_version == ""
    assert result.configuration_identity.scan_mode == "vuln"
    assert result.configuration_identity.fixed_flags == list(trivy.TRIVY_FIXED_FLAGS)


@pytest.mark.parametrize(
    "scanner,key",
    [
        (trivy.scan_dependencies, "Vulnerabilities"),
        (trivy.scan_artifact, "Misconfigurations"),
        (trivy.scan_secrets, "Secrets"),
    ],
)
@pytest.mark.parametrize("rows", [{"unexpected": "object"}, [None]])
def test_trivy_rejects_malformed_finding_collections(
    monkeypatch, tmp_path, scanner, key, rows
):
    monkeypatch.setattr(trivy.shutil, "which", lambda _name: "/usr/bin/trivy")
    result = scanner(
        LocalScanArgs(target="."),
        target=tmp_path,
        config=_config(tmp_path),
        process_runner=_runner(json.dumps({"Results": [{key: rows}]}), return_code=0),
    )
    assert not result.ok and result.status == "failed"
    assert result.error and result.error.code == "INVALID_RESPONSE"
    assert result.findings == []


@pytest.mark.parametrize("payload", [{"Results": [42]}, {"Results": [{}, 42]}])
def test_trivy_rejects_malformed_result_rows(monkeypatch, tmp_path, payload):
    monkeypatch.setattr(trivy.shutil, "which", lambda _name: "/usr/bin/trivy")
    result = trivy.scan_dependencies(
        LocalScanArgs(target="."),
        target=tmp_path,
        config=_config(tmp_path),
        process_runner=_runner(json.dumps(payload), return_code=0),
    )
    assert not result.ok and result.status == "failed"
    assert result.error and result.error.code == "INVALID_RESPONSE"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"Results": []},
        {"Results": [{}]},
        {"Results": [{"Vulnerabilities": None}]},
        {"Results": [{"Vulnerabilities": []}]},
    ],
)
def test_trivy_preserves_empty_and_null_findings(monkeypatch, tmp_path, payload):
    monkeypatch.setattr(trivy.shutil, "which", lambda _name: "/usr/bin/trivy")
    result = trivy.scan_dependencies(
        LocalScanArgs(target="."),
        target=tmp_path,
        config=_config(tmp_path),
        process_runner=_runner(json.dumps(payload), return_code=0),
    )
    assert result.ok and result.status == "completed" and result.findings == []


@pytest.mark.parametrize(
    "second",
    [
        {"InstalledVersion": "2.0.0", "PkgPath": "node_modules/example"},
        {"InstalledVersion": "1.0.0", "PkgPath": "nested/node_modules/example"},
    ],
)
def test_dependency_occurrences_have_distinct_stable_ids(monkeypatch, tmp_path, second):
    monkeypatch.setattr(trivy.shutil, "which", lambda _name: "/usr/bin/trivy")
    first = {
        "VulnerabilityID": "CVE-2026-0001",
        "PkgName": "example",
        "InstalledVersion": "1.0.0",
        "PkgPath": "node_modules/example",
    }
    payload = json.dumps(
        {
            "Results": [
                {
                    "Target": "package-lock.json",
                    "Vulnerabilities": [first, {**first, **second}, first],
                }
            ]
        }
    )
    result = trivy.scan_dependencies(
        LocalScanArgs(target="."),
        target=tmp_path,
        config=_config(tmp_path),
        process_runner=_runner(payload),
    )
    assert result.status == "completed" and result.total_findings == 3
    ids = [finding.finding_id for finding in result.findings]
    assert ids[0] != ids[1]
    assert ids[0] == ids[2]


@pytest.mark.parametrize("return_code", [1, 2])
def test_trivy_failure_without_json_keeps_redacted_diagnostic(
    monkeypatch, tmp_path, return_code
):
    monkeypatch.setattr(trivy.shutil, "which", lambda _name: "/usr/bin/trivy")
    result = trivy.scan_dependencies(
        LocalScanArgs(target="."),
        target=tmp_path,
        config=_config(tmp_path),
        process_runner=_runner(
            "",
            return_code=return_code,
            stderr="DB error: --skip-db-update cannot be specified on the first run; password=synthetic-secret",
        ),
    )
    assert not result.ok and result.status == "failed"
    assert result.error and result.error.code == "EXEC_ERROR"
    assert result.error.exit_code == return_code
    assert "--skip-db-update" in result.error.message
    assert "synthetic-secret" not in result.error.message
    assert "invalid JSON" not in result.error.message


def test_trivy_cancelled_scan_keeps_its_terminal_status(monkeypatch, tmp_path):
    monkeypatch.setattr(trivy.shutil, "which", lambda _name: "/usr/bin/trivy")

    def cancelled(argv, **kwargs):
        if "--version" in argv:
            return ScannerProcessResult(return_code=0, stdout="Version: 0.70.0")
        return ScannerProcessResult(
            return_code=-15, cancelled=True, stderr="terminated"
        )

    result = trivy.scan_dependencies(
        LocalScanArgs(target="."),
        target=tmp_path,
        config=_config(tmp_path),
        process_runner=cancelled,
    )
    assert result.status == "cancelled"
    assert result.error and result.error.code == "EXEC_ERROR"
    assert result.error.exit_code == -15
    assert result.error.message == "Trivy scan was cancelled"
