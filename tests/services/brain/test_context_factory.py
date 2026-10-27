from unittest import mock

from openminion.services.brain.factory.adapter import create_context_api


class _SessionStore:
    def get_slice(self, *, session_id: str, purpose: str, limits: dict[str, int]):
        del purpose, limits
        return {
            "session_id": session_id,
            "slice_version": "test:v1",
            "summary": {"summary_short": ""},
            "recent_turns": [],
            "open_tasks": [],
            "active_state": {},
            "recent_tool_events": [],
        }


def test_context_factory_registers_episode_retriever_in_services_layer() -> None:
    retrieve_api = object()

    with mock.patch(
        "openminion.services.brain.factory.adapter.create_context_adapter",
        return_value=object(),
    ) as module_factory:
        create_context_api(
            mode="auto",
            session_store=object(),
            system_prompt="system",
            retrieve_api=retrieve_api,
        )

    registry = module_factory.call_args.kwargs["plugin_registry"]
    retriever = registry.get_retriever("compacted_session_episodes")
    assert retriever is not None
    assert retriever._retrieve_api is retrieve_api


def test_context_factory_omits_episode_retriever_without_retrievectl() -> None:
    with mock.patch(
        "openminion.services.brain.factory.adapter.create_context_adapter",
        return_value=object(),
    ) as module_factory:
        create_context_api(
            mode="auto",
            session_store=object(),
            system_prompt="system",
        )

    assert module_factory.call_args.kwargs["plugin_registry"] is None


def test_context_factory_wires_episode_retriever_into_real_context_service() -> None:
    class RetrieveAPI:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []

        def retrieve(self, query: str, **kwargs):
            self.calls.append((query, kwargs))
            return [
                {
                    "ref_type": "episode",
                    "ref_id": "session://s-1/episode-1",
                    "text_snippet": "Durable saffron comet decision.",
                    "score": 0.9,
                    "retrieval_strategy": "contextual",
                }
            ]

    retrieve_api = RetrieveAPI()
    adapter = create_context_api(
        mode="auto",
        session_store=_SessionStore(),
        system_prompt="Test system prompt.",
        artifactctl=object(),
        retrieve_api=retrieve_api,
    )
    try:
        payload = adapter.build(
            session_id="s-1",
            agent_id="a-1",
            purpose="act",
            budget={"max_tokens": 512},
            hints={"query": "continue", "continuity_query": "saffron comet"},
        )
    finally:
        adapter.close()

    assert retrieve_api.calls == [
        (
            "saffron comet",
            {
                "purpose": "act",
                "scope": {"session_id": "s-1"},
                "k": 10,
                "strategy": "contextual",
                "filters": {
                    "scope_keys": ["session:s-1"],
                    "types": ["episode"],
                },
            },
        )
    ]
    assert "Durable saffron comet decision." in "\n".join(
        str(message.get("content", "")) for message in payload["messages"]
    )
