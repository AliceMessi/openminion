"""Operator-procedure fixtures, not runtime project admission enforcement."""

import json

import pytest

from openminion.base.redaction import redact_sensitive_text
from openminion.modules.artifact.errors import ArtifactCtlError
from openminion.tools.security.review_schemas import SecurityFindingReview
from openminion.tools.security.schemas import SecurityAuditReport
from openminion.tools.security.plugin import _h_review_report
from tests.tools.security.test_audit_report import audit_context as audit_context
from tests.tools.security.test_review_report import _initial, _source, _reviewer


def operator_handoff(
    ctx, source_ref, review_ref, *, base, before_exit_code, paths, commands
):
    """Apply the documented checklist in a fixture before calling project launch."""
    source = SecurityAuditReport.model_validate_json(
        ctx.artifactctl.read_bytes(source_ref)
    )
    review = SecurityFindingReview.model_validate_json(
        ctx.artifactctl.read_bytes(review_ref)
    )
    assert not ctx.artifactctl.get(source_ref).deleted_at
    assert not ctx.artifactctl.get(review_ref).deleted_at
    assert review.source_report_ref == source_ref and review.review_kind == "initial"
    assert review.scope.target_revision == source.scope.target_revision == base
    assert (
        ctx.artifactctl.get(review_ref).meta_json["tool_name"]
        == "security.review_report"
    )
    assert review.reviewer_agent_id != ctx.artifactctl.get(source_ref).agent_id
    assert len(review.remediation_brief) == 1
    finding = review.remediation_brief[0]
    assert {location.path for location in finding.locations} <= set(paths)
    assert before_exit_code == 1 and commands
    handoff = json.dumps(
        {
            "report_ref": source_ref,
            "review_ref": review_ref,
            "finding_id": finding.finding_id,
            "review_id": review.review_id,
            "remediation_brief": finding.model_dump(mode="json", exclude_none=True),
            "verification_timeout_seconds": 20,
            "delivery_expectations": "Passing verifier and independent child review before parent acceptance.",
            "base_revision": base,
            "allowed_paths": paths,
            "verification_commands": commands,
            "failing_before_exit_code": before_exit_code,
            "operator_approved": True,
        }
    )
    redacted, count = redact_sensitive_text(handoff)
    assert not count and len(redacted) <= 4000
    return redacted


@pytest.mark.parametrize(
    "invalid",
    [
        "unreviewed",
        "deleted",
        "stale",
        "unaccepted",
        "passing_before",
        "scope",
        "missing_verifier",
        "overflow",
        "redaction",
    ],
)
def test_operator_withholds_launch_for_incomplete_repair_handoff(
    audit_context, invalid
):
    ctx, base = audit_context
    source = _source(ctx, base)
    args = _initial(source)
    if invalid == "unaccepted":
        args["finding_reviews"][0] = {
            "finding_id": "F-1",
            "decision": "rejected",
            "rationale": "Unsupported.",
        }
    if invalid == "overflow":
        args["finding_reviews"][0]["verification_criteria"] = ["x" * 1000] * 5
    review = _h_review_report(args, _reviewer(ctx))["review_ref"]
    if invalid == "deleted":
        ctx.artifactctl.delete(review)
    options = dict(
        base=base,
        before_exit_code=1,
        paths=["app.py"],
        commands=["python -B verify.py"],
    )
    if invalid == "unreviewed":
        review = source
    if invalid == "stale":
        options["base"] = "b" * 40
    if invalid == "passing_before":
        options["before_exit_code"] = 0
    if invalid == "scope":
        options["paths"] = ["other.py"]
    if invalid == "missing_verifier":
        options["commands"] = []
    if invalid == "redaction":
        options["commands"] = ["verify --token=fixture-secret"]
    with pytest.raises((AssertionError, ValueError, ArtifactCtlError)):
        operator_handoff(ctx, source, review, **options)
