from __future__ import annotations

import pytest

from openminion.services.context.adapter import CompactedEpisodeRetriever


class _RetrieveAPI:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def retrieve(self, query: str, **kwargs):
        self.calls.append((query, kwargs))
        return [
            {
                "ref_type": "episode",
                "ref_id": "session://s-1/episode-1",
                "text_snippet": "The release target is canary.",
                "score": 0.88,
                "retrieval_strategy": "contextual",
            }
        ]


def test_compacted_episode_retriever_uses_exact_session_scope() -> None:
    retrieve_api = _RetrieveAPI()
    retriever = CompactedEpisodeRetriever(retrieve_api)

    items = retriever.retrieve(
        session_id="s-1",
        query="release target",
        k=4,
        filters={"query_role": "continuity"},
    )

    assert retrieve_api.calls == [
        (
            "release target",
            {
                "purpose": "act",
                "scope": {"session_id": "s-1"},
                "k": 4,
                "strategy": "contextual",
                "filters": {
                    "scope_keys": ["session:s-1"],
                    "types": ["episode"],
                },
            },
        )
    ]
    assert [item.model_dump() for item in items] == [
        {
            "ref": "session://s-1/episode-1",
            "content": "The release target is canary.",
            "score": 0.88,
            "source": "compacted_session_episodes",
            "metadata": {
                "ref_type": "episode",
                "retrieval_strategy": "contextual",
            },
        }
    ]


@pytest.mark.parametrize("query_role", ["current", "", None])
def test_compacted_episode_retriever_ignores_non_continuity_roles(
    query_role: str | None,
) -> None:
    retrieve_api = _RetrieveAPI()
    retriever = CompactedEpisodeRetriever(retrieve_api)

    assert (
        retriever.retrieve(
            session_id="s-1",
            query="release target",
            k=4,
            filters={"query_role": query_role},
        )
        == []
    )
    assert retrieve_api.calls == []


@pytest.mark.parametrize(
    ("session_id", "query"),
    [("", "release target"), ("   ", "release target"), ("s-1", ""), ("s-1", "  ")],
)
def test_compacted_episode_retriever_requires_session_and_query(
    session_id: str,
    query: str,
) -> None:
    retrieve_api = _RetrieveAPI()
    retriever = CompactedEpisodeRetriever(retrieve_api)

    assert (
        retriever.retrieve(
            session_id=session_id,
            query=query,
            k=4,
            filters={"query_role": "continuity"},
        )
        == []
    )
    assert retrieve_api.calls == []


def test_compacted_episode_retriever_leaves_failure_to_contextctl() -> None:
    class FailingRetrieveAPI:
        def retrieve(self, query: str, **kwargs):
            del query, kwargs
            raise RuntimeError("retrieve unavailable")

    retriever = CompactedEpisodeRetriever(FailingRetrieveAPI())

    with pytest.raises(RuntimeError, match="retrieve unavailable"):
        retriever.retrieve(
            session_id="s-1",
            query="release target",
            k=4,
            filters={"query_role": "continuity"},
        )
