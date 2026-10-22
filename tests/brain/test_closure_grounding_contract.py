"""Closure judge and final-answer repair requests: candidate versus execution facts."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from openminion.modules.brain.adapters.a2a import LocalA2AAdapter
from openminion.modules.brain.adapters.factory import create_context_adapter
from openminion.modules.brain.adapters.llm import LlmctlAdapter
from openminion.modules.brain.adapters.memory import LocalMemoryAdapter
from openminion.modules.brain.adapters.policy import LocalPolicyAdapter
from openminion.modules.brain.adapters.session import LocalSessionStore
from openminion.modules.brain.adapters.tool import LocalToolAdapter
from openminion.modules.brain.loop.tools.iteration.helpers import (
    _tool_result_payload_from_action,
)
from openminion.modules.brain.runner import BrainRunner, RunnerOptions
from openminion.modules.brain.schemas import (
    ActionResult,
    AgentBudgets,
    AgentDefaults,
    AgentProfile,
    ClosureJudgment,
    FreshnessContract,
    FreshnessObligations,
    LLMProfiles,
)
from openminion.modules.llm.schemas import LLMRequest, LLMResponse, ToolCall, UsageInfo

_TOOL_OUTPUT = "order A-104: ready for pickup, pickup code ZEBRA-42"
_SUPPORTED_CANDIDATE = "Order A-104 is ready for pickup. Your pickup code is ZEBRA-42."
_UNSUPPORTED_CANDIDATE = "Order A-104 is in transit and arrives on Friday."
_CORRECTED_ANSWER = (
    "Order A-104 is ready for pickup, not in transit. Your pickup code is ZEBRA-42."
)
_FAILURE_ANSWER = "I could not retrieve order A-104: the order lookup failed."
_REFUSAL = "I do not have access to your order information."

_CANDIDATE_CLAUSES = (
    "Treat action_summary as the candidate answer under review",
    "preserve requested facts and identifiers that the execution facts support",
    "correct candidate claims they do not support using those execution facts",
    "keep truthful failure or uncertainty wording when the execution facts are "
    "insufficient",
    "Evaluate freshness_obligations against the execution facts supplied for this turn",
    "a successful tool call does not by itself satisfy every obligation",
)
_TOOLS_RAN_CLAUSE = (
    "do not infer that execution tools were unavailable when the supplied execution "
    "facts show they ran"
)


class _ScriptedClosureClient:
    def __init__(self, *replies: dict[str, Any]) -> None:
        self.replies = list(replies)
        self.requests: list[LLMRequest] = []

    def call(self, req: LLMRequest) -> LLMResponse:
        self.requests.append(req)
        if len(self.requests) > len(self.replies):
            raise AssertionError("unexpected extra closure call")
        return LLMResponse(
            ok=True,
            provider="test",
            model=req.model,
            output_text="",
            tool_calls=[
                ToolCall(
                    name="submit_output",
                    arguments=self.replies[len(self.requests) - 1],
                )
            ],
            usage=UsageInfo(total_tokens=1),
            finish_reason="tool_calls",
        )

    def schema_titles(self) -> list[str]:
        return [
            str((request.tools or [])[0].input_schema.get("title", ""))
            for request in self.requests
        ]


def _profile() -> AgentProfile:
    return AgentProfile(
        agent_id="grounding-agent",
        role="general",
        llm_profiles=LLMProfiles(
            decide_model="decide-default",
            plan_model="plan-default",
            act_model=None,
            reflect_model="judge-model",
            summarize_model="summarize-default",
        ),
        budgets=AgentBudgets(
            max_ticks_per_user_turn=3,
            max_tool_calls=2,
            max_a2a_calls=1,
            max_total_llm_tokens=4000,
            max_elapsed_ms=10000,
        ),
        defaults=AgentDefaults(),
    )


def _lookup_order_result(*, ok: bool = True) -> dict[str, Any]:
    if not ok:
        return {
            "tool_name": "lookup_order",
            "ok": False,
            "verified": False,
            "error_code": "upstream_unavailable",
            "data": {"status": "error", "data": "order service unavailable"},
        }
    return _tool_result_payload_from_action(
        call_id="call-lookup-order",
        tool_name="lookup_order",
        action_result=ActionResult(
            command_id="call-lookup-order",
            status="success",
            summary="Tool executed successfully",
            outputs={"data": _TOOL_OUTPUT},
        ),
    )


def _evaluate_closure(
    client: _ScriptedClosureClient,
    *,
    candidate: str = _SUPPORTED_CANDIDATE,
    tool_results: list[dict[str, Any]] | None = None,
    freshness: bool = False,
) -> ClosureJudgment:
    if tool_results is None:
        tool_results = [_lookup_order_result()]
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        session = LocalSessionStore(root / "sessions")
        context_api = create_context_adapter(mode="auto", session_store=session)
        runner = BrainRunner(
            profile=_profile(),
            session_api=session,
            context_api=context_api,
            llm_api=LlmctlAdapter(client),
            tool_api=LocalToolAdapter(),
            a2a_api=LocalA2AAdapter(),
            memory_api=LocalMemoryAdapter(root / "memory"),
            policy_api=LocalPolicyAdapter(),
            options=RunnerOptions(metactl_enabled=False),
        )
        state = runner._load_or_init_state("closure-grounding")
        state.goal = "Check order A-104"
        if freshness:
            state.freshness_contract = FreshnessContract(
                intent="order_status_check",
                domain="shopping",
                time_sensitive=True,
                needs_live_data=True,
                answer_mode="browse_then_answer",
                reason="Order status is live data.",
            )
            state.freshness_obligations = FreshnessObligations(
                require_live_data=True,
                require_explicit_failure_wording=True,
                answer_mode="browse_then_answer",
            )
        try:
            return runner._evaluate_turn_closure(
                state=state,
                action_result=ActionResult(
                    command_id="cmd-act-loop",
                    status="success",
                    summary=candidate,
                    outputs={"tool_results": tool_results} if tool_results else {},
                ),
                logger=MagicMock(),
                completion_reason="act_adaptive_final_text",
            )
        finally:
            context_api.close()


def _rendered_lines(request: LLMRequest) -> list[str]:
    return [
        line for message in request.messages for line in message.content.splitlines()
    ]


def _rendered_line(request: LLMRequest, label: str) -> str:
    matches = [
        line for line in _rendered_lines(request) if line.startswith(f"{label}: ")
    ]
    assert len(matches) == 1, f"expected one rendered {label!r} line"
    return matches[0].removeprefix(f"{label}: ")


def _rendered_contract(request: LLMRequest, key: str) -> str:
    return str(json.loads(_rendered_line(request, "style_overrides"))[key])


def test_rendered_judge_request_states_candidate_and_evidence_contract() -> None:
    client = _ScriptedClosureClient(
        {
            "satisfied": True,
            "reason": "lookup_order returned the pickup code",
            "next_action": "close",
            "final_answer": _SUPPORTED_CANDIDATE,
        }
    )

    _evaluate_closure(client, freshness=True)

    assert client.schema_titles() == ["ClosureJudgment"]
    request = client.requests[0]
    assert _rendered_line(request, "action_summary") == _SUPPORTED_CANDIDATE
    assert _TOOL_OUTPUT in _rendered_line(request, "closure_action_outputs")
    obligations = json.loads(_rendered_line(request, "freshness_obligations"))
    assert obligations["require_live_data"] is True
    assert obligations["require_explicit_failure_wording"] is True
    contract = _rendered_contract(request, "closure_gate_contract")
    assert (
        "final_answer should be a concise user-facing answer grounded in the "
        "execution facts" in contract
    )
    for clause in (*_CANDIDATE_CLAUSES, _TOOLS_RAN_CLAUSE):
        assert clause in contract


def test_rendered_repair_request_states_candidate_and_evidence_contract() -> None:
    client = _ScriptedClosureClient(
        {"satisfied": True, "reason": "order found", "next_action": "close"},
        {"final_answer": _SUPPORTED_CANDIDATE},
    )

    _evaluate_closure(client, freshness=True)

    assert client.schema_titles() == ["ClosureJudgment", "_ClosureFinalAnswerRepair"]
    request = client.requests[1]
    assert _rendered_line(request, "action_summary") == _SUPPORTED_CANDIDATE
    assert _TOOL_OUTPUT in _rendered_line(request, "closure_action_outputs")
    assert json.loads(_rendered_line(request, "freshness_obligations"))[
        "require_live_data"
    ]
    contract = _rendered_contract(request, "closure_final_answer_repair_contract")
    assert "closure_action_outputs and any recent tool_results" in contract
    for clause in (*_CANDIDATE_CLAUSES, _TOOLS_RAN_CLAUSE):
        assert clause in contract


@pytest.mark.parametrize(
    ("candidate", "model_answer"),
    [
        (_SUPPORTED_CANDIDATE, _SUPPORTED_CANDIDATE),
        (_UNSUPPORTED_CANDIDATE, _CORRECTED_ANSWER),
    ],
    ids=["supported_candidate_kept", "unsupported_candidate_corrected"],
)
def test_judge_close_returns_model_answer_reviewed_against_evidence(
    candidate: str, model_answer: str
) -> None:
    client = _ScriptedClosureClient(
        {
            "satisfied": True,
            "reason": "checked the candidate against lookup_order",
            "next_action": "close",
            "final_answer": model_answer,
        }
    )

    judgment = _evaluate_closure(client, candidate=candidate)

    request = client.requests[0]
    assert _rendered_line(request, "action_summary") == candidate
    assert _TOOL_OUTPUT in _rendered_line(request, "closure_action_outputs")
    assert judgment.satisfied is True
    assert judgment.next_action == "close"
    assert judgment.final_answer == model_answer


@pytest.mark.parametrize(
    ("tool_results", "verdict"),
    [
        (
            [],
            {
                "satisfied": False,
                "reason": "no order lookup ran",
                "next_action": "continue",
                "final_answer": None,
            },
        ),
        (
            [_lookup_order_result(ok=False)],
            {
                "satisfied": True,
                "reason": "lookup_order failed; reported truthfully",
                "next_action": "close",
                "final_answer": _FAILURE_ANSWER,
            },
        ),
    ],
    ids=["missing_tool_evidence", "failed_tool_evidence"],
)
def test_judge_request_without_supporting_evidence_keeps_model_verdict(
    tool_results: list[dict[str, Any]], verdict: dict[str, Any]
) -> None:
    client = _ScriptedClosureClient(verdict)

    judgment = _evaluate_closure(client, tool_results=tool_results)

    request = client.requests[0]
    rendered = "\n".join(_rendered_lines(request))
    assert _rendered_line(request, "action_summary") == _SUPPORTED_CANDIDATE
    assert _TOOL_OUTPUT not in rendered
    if tool_results:
        evidence = json.loads(_rendered_line(request, "closure_action_outputs"))
        assert evidence["tool_results"][0]["ok"] is False
    else:
        assert not any(
            line.startswith("closure_action_outputs: ")
            for line in _rendered_lines(request)
        )
    contract = _rendered_contract(request, "closure_gate_contract")
    for clause in _CANDIDATE_CLAUSES:
        assert clause in contract
    assert judgment.next_action == verdict["next_action"]
    assert judgment.final_answer == verdict["final_answer"]
    assert judgment.final_answer != _SUPPORTED_CANDIDATE


@pytest.mark.parametrize(
    ("satisfied", "next_action", "final_answer"),
    [
        (True, "close", _SUPPORTED_CANDIDATE),
        (False, "continue", None),
        (False, "replan", None),
    ],
)
def test_judge_verdict_is_model_owned_for_each_disposition(
    satisfied: bool, next_action: str, final_answer: str | None
) -> None:
    client = _ScriptedClosureClient(
        {
            "satisfied": satisfied,
            "reason": "verdict",
            "next_action": next_action,
            "final_answer": final_answer,
        }
    )

    judgment = _evaluate_closure(client)

    assert client.schema_titles() == ["ClosureJudgment"]
    assert judgment.satisfied is satisfied
    assert judgment.next_action == next_action
    assert judgment.final_answer == final_answer


def test_incomplete_verdict_never_becomes_the_turn_answer() -> None:
    client = _ScriptedClosureClient({"final_answer": _REFUSAL})

    try:
        judgment: ClosureJudgment | None = _evaluate_closure(client)
    except RuntimeError:
        judgment = None

    assert client.schema_titles() == ["ClosureJudgment"]
    if judgment is not None:
        assert judgment.next_action != "close"
        assert judgment.final_answer is None


def test_missing_final_answer_repair_returns_model_answer() -> None:
    client = _ScriptedClosureClient(
        {"satisfied": True, "reason": "order found", "next_action": "close"},
        {"final_answer": _CORRECTED_ANSWER},
    )

    judgment = _evaluate_closure(client, candidate=_UNSUPPORTED_CANDIDATE)

    assert client.schema_titles() == ["ClosureJudgment", "_ClosureFinalAnswerRepair"]
    assert _rendered_line(client.requests[1], "action_summary") == (
        _UNSUPPORTED_CANDIDATE
    )
    assert judgment.satisfied is True
    assert judgment.next_action == "close"
    assert judgment.final_answer == _CORRECTED_ANSWER


@pytest.mark.parametrize(
    "repair_reply",
    [{"final_answer": ""}, {}],
    ids=["empty_final_answer", "invalid_repair_output"],
)
def test_missing_final_answer_repair_failure_does_not_close(
    repair_reply: dict[str, Any],
) -> None:
    client = _ScriptedClosureClient(
        {"satisfied": True, "reason": "order found", "next_action": "close"},
        repair_reply,
        repair_reply,
    )

    judgment = _evaluate_closure(client)

    titles = client.schema_titles()
    assert titles[0] == "ClosureJudgment"
    assert titles[1:] and set(titles[1:]) == {"_ClosureFinalAnswerRepair"}
    assert judgment.satisfied is False
    assert judgment.next_action == "continue"
    assert judgment.final_answer is None
    assert "closure_missing_final_answer" in judgment.reason
