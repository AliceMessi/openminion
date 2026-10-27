from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

import pytest

from openminion.modules.skill.cli import main
from openminion.modules.skill.proposal import SkillProposal, SkillProposalDraft
from openminion.modules.skill.proposal.queue import (
    create_proposal,
    record_proposal_review,
)
from openminion.modules.skill.runtime.skill import Skill


def _config_path(tmp_path: Path, **overrides: object) -> Path:
    db = tmp_path / "skill.db"
    cfg = tmp_path / "skill.json"
    skill_config = {
        "sqlite_path": str(db),
        "blob_root": str(tmp_path / "blob"),
        "fallback_root": str(tmp_path / "fallback"),
        "wal": False,
        **overrides,
    }
    cfg.write_text(
        json.dumps(
            {
                "skill": skill_config,
            }
        ),
        encoding="utf-8",
    )
    return cfg


def _run_cli(argv: list[str]) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(argv)
    assert rc == 0, f"CLI exit code: {rc}; stdout={buf.getvalue()!r}"
    return buf.getvalue()


def _seed_proposal(tmp_path: Path, *, proposal_id: str = "scsp-cli-1") -> str:
    cfg = _config_path(tmp_path)
    ctl = Skill(str(cfg))
    try:
        create_proposal(
            ctl.store,
            SkillProposal(
                proposal_id=proposal_id,
                source_task_shape_ref="task_shape:cli|cli|cli",
                proposed_skill_definition=SkillProposalDraft(
                    name="cli-playbook",
                    display_name="CLI Playbook",
                    short_description="Seeded for SCSP CLI test.",
                    tools=[],
                    tags=["cli"],
                    risk_class="low",
                    applies_to={"intents": ["cli"], "steps": []},
                    inputs_schema=[],
                    verification_rules=[],
                ),
                evidence_refs=[],
                proposer_policy_id="skill_promotion_cadence_v1",
                proposed_at="",
            ),
        )
    finally:
        ctl.close()
    return proposal_id


def test_cli_suggestion_inbox_lists_pending(tmp_path: Path) -> None:
    _seed_proposal(tmp_path)
    cfg = _config_path(tmp_path)
    out = _run_cli(["--config", str(cfg), "suggestion-inbox"])
    payload = json.loads(out)
    assert payload["ok"] is True
    suggestions = payload["suggestions"]
    assert len(suggestions) == 1
    assert suggestions[0]["proposal_id"] == "scsp-cli-1"
    # Structural projection — no quality fields.
    forbidden = {"quality", "value", "score", "confidence"}
    assert forbidden.isdisjoint(suggestions[0].keys())


def test_cli_suggestion_status_shows_zero_before_pass(tmp_path: Path) -> None:
    _seed_proposal(tmp_path)
    cfg = _config_path(tmp_path)
    out = _run_cli(["--config", str(cfg), "suggestion-status"])
    payload = json.loads(out)
    assert payload["ok"] is True
    status = payload["status"]
    assert status["surfaced_count"] == 0
    assert status["pending_count"] == 1


def test_cli_surface_pass_uses_config_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _config_path(
        tmp_path,
        suggestion_batch_cap=2,
        suggestion_cooldown_seconds=45,
        suggestion_min_age_seconds=15,
    )
    captured: dict[str, int] = {}

    def _surface(_store: object, **kwargs: int) -> SimpleNamespace:
        captured.update(kwargs)
        return SimpleNamespace(surfaced=[], auto_dismissed=[], pending_remaining=0)

    monkeypatch.setattr(
        "openminion.modules.skill.suggestion.run_suggestion_surface_pass", _surface
    )

    _run_cli(["--config", str(cfg), "suggestion-surface-pass"])

    assert captured == {
        "batch_cap": 2,
        "cooldown_seconds": 45,
        "min_age_seconds": 15,
    }


def test_cli_surface_pass_arguments_override_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _config_path(
        tmp_path,
        suggestion_batch_cap=2,
        suggestion_cooldown_seconds=45,
        suggestion_min_age_seconds=15,
    )
    captured: dict[str, int] = {}

    def _surface(_store: object, **kwargs: int) -> SimpleNamespace:
        captured.update(kwargs)
        return SimpleNamespace(surfaced=[], auto_dismissed=[], pending_remaining=0)

    monkeypatch.setattr(
        "openminion.modules.skill.suggestion.run_suggestion_surface_pass", _surface
    )

    _run_cli(
        [
            "--config",
            str(cfg),
            "suggestion-surface-pass",
            "--batch-cap",
            "4",
            "--cooldown-seconds",
            "90",
            "--min-age-seconds",
            "30",
        ]
    )

    assert captured == {
        "batch_cap": 4,
        "cooldown_seconds": 90,
        "min_age_seconds": 30,
    }


def test_cli_suggestion_surface_pass_writes_audit_and_status_reflects_it(
    tmp_path: Path,
) -> None:
    _seed_proposal(tmp_path)
    cfg = _config_path(tmp_path)
    pass_out = _run_cli(["--config", str(cfg), "suggestion-surface-pass"])
    pass_payload = json.loads(pass_out)
    assert pass_payload["ok"] is True
    assert len(pass_payload["surfaced"]) == 1

    status_out = _run_cli(["--config", str(cfg), "suggestion-status"])
    status_payload = json.loads(status_out)["status"]
    assert status_payload["surfaced_count"] == 1
    assert status_payload["last_surfaced_at"]


def test_cli_suggestion_status_reflects_review_outcomes(tmp_path: Path) -> None:
    cfg = _config_path(tmp_path)
    ctl = Skill(str(cfg))
    try:
        for idx in range(2):
            create_proposal(
                ctl.store,
                SkillProposal(
                    proposal_id=f"scsp-cli-out-{idx}",
                    source_task_shape_ref=f"task_shape:out|out|out-{idx}",
                    proposed_skill_definition=SkillProposalDraft(
                        name=f"distinct-playbook-out-{idx}",
                        display_name=f"Out {idx}",
                        short_description="x",
                        tools=[],
                        tags=[f"capability-out-{idx}"],
                        risk_class="low",
                        applies_to={
                            "intents": [f"intent-out-{idx}"],
                            "steps": [],
                        },
                        inputs_schema=[],
                        verification_rules=[],
                    ),
                    evidence_refs=[],
                    proposer_policy_id="skill_promotion_cadence_v1",
                    proposed_at="",
                ),
            )
        record_proposal_review(
            ctl.store,
            proposal_id="scsp-cli-out-0",
            reviewer_id="operator-cli-out",
            review_policy_id="scsp_cli",
            criterion_decisions=[
                {
                    "criterion_id": "fit",
                    "status": "accepted",
                    "comment": "ok",
                }
            ],
        )
        record_proposal_review(
            ctl.store,
            proposal_id="scsp-cli-out-1",
            reviewer_id="operator-cli-out",
            review_policy_id="scsp_cli",
            criterion_decisions=[
                {
                    "criterion_id": "fit",
                    "status": "rejected",
                    "comment": "no",
                }
            ],
        )
    finally:
        ctl.close()
    out = _run_cli(["--config", str(cfg), "suggestion-status"])
    status = json.loads(out)["status"]
    assert status["accepted_count"] == 1
    assert status["rejected_count"] == 1
