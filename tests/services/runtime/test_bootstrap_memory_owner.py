"""The bootstrap memory fallback closes with the gateway service or caller that built it."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from openminion import Agent, APIRuntime
from openminion.base.channel import ChannelRegistry
from openminion.base.config import OpenMinionConfig, save_config
from openminion.modules.artifact.control import ArtifactCtl
from openminion.modules.memory.storage.audit import SQLiteMemoryAuditSink
from openminion.modules.memory.storage.sqlite.store import SQLiteMemoryStore
from openminion.modules.storage.runtime.idempotency_store import IdempotencyStore
from openminion.modules.storage.runtime.migrations import migrate_database
from openminion.modules.storage.runtime.session_store import SessionStore
from openminion.modules.storage.runtime.sqlite import connect_database
from openminion.services.runtime import bootstrap
from openminion.services.runtime import memory as runtime_memory
from tests._csc_fixtures import _csc_install_default_agent

_TURN = {
    "session_id": "s1",
    "run_id": "r1",
    "request_id": "q1",
    "channel": "console",
    "target": "local-user",
    "user_message": "Remember: the probe colour is green.",
    "assistant_message": "Noted.",
}


class _ReplyAgent:
    async def run_turn(self, message, history=None, **_kwargs):  # noqa: ANN001
        del history, _kwargs
        return SimpleNamespace(
            text="Noted.", channel=message.channel, target=message.target, metadata={}
        )


class _Scheduler:
    def __init__(self) -> None:
        self.alive = True

    def bind_record_source(self, record_source: Any) -> None:
        del record_source

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None

    def is_alive(self) -> bool:
        return self.alive


@dataclass
class _Created:
    """What the memory assemblies built during a test created, and its closes."""

    scheduler: _Scheduler | None = None
    assemblies: list[Any] = field(default_factory=list)
    stores: list[Any] = field(default_factory=list)
    sinks: list[SQLiteMemoryAuditSink] = field(default_factory=list)
    closed: list[int] = field(default_factory=list)

    def resources(self) -> list[Any]:
        [assembly] = self.assemblies
        return [*self.stores, *self.sinks, assembly.artifactctl]

    def close_counts(self) -> list[int]:
        return [self.closed.count(id(item)) for item in self.resources()]

    def sinks_open(self) -> list[bool]:
        return [sink._conn is not None for sink in self.sinks]


@pytest.fixture
def created(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Created]:
    record = _Created()
    build_assembly = runtime_memory.build_memory_v2_runtime_assembly
    resolve_backend = runtime_memory.resolve_memory_backend
    create_sink = runtime_memory.SQLiteMemoryAuditSink
    patched: set[type] = set()

    def count_closes(cls: type) -> None:
        if cls in patched:
            return
        patched.add(cls)
        original = cls.close

        def close(self):
            record.closed.append(id(self))
            return original(self)

        monkeypatch.setattr(cls, "close", close)

    def record_assembly(**kwargs):
        if record.scheduler is not None:
            kwargs["scheduler"] = record.scheduler
        assembly = build_assembly(**kwargs)
        record.assemblies.append(assembly)
        return assembly

    def record_backend(**kwargs):
        resolved = resolve_backend(**kwargs)
        count_closes(type(resolved.store))
        record.stores.append(resolved.store)
        return resolved

    def record_sink(db_path):
        sink = create_sink(db_path)
        record.sinks.append(sink)
        return sink

    count_closes(SQLiteMemoryAuditSink)
    count_closes(ArtifactCtl)
    monkeypatch.setattr(
        runtime_memory, "build_memory_v2_runtime_assembly", record_assembly
    )
    monkeypatch.setattr(runtime_memory, "resolve_memory_backend", record_backend)
    monkeypatch.setattr(runtime_memory, "SQLiteMemoryAuditSink", record_sink)
    yield record
    for assembly in record.assemblies:
        for item in (*record.sinks, assembly.artifactctl):
            if item is not None and id(item) not in record.closed:
                item.close()


@pytest.fixture
def runtime_storage(tmp_path: Path) -> Iterator[SimpleNamespace]:
    database_path = tmp_path / "state" / "openminion.db"
    migrate_database(database_path)
    connection = connect_database(database_path)
    try:
        yield SimpleNamespace(
            path=database_path,
            sessions=SessionStore(connection),
            idempotency=IdempotencyStore(connection),
        )
    finally:
        connection.close()


def _config() -> OpenMinionConfig:
    config = OpenMinionConfig()
    _csc_install_default_agent(config, name="main", provider="echo")
    config.runtime.memory_enabled = True
    config.runtime.memory_provider = "memory_v2"
    return config


def _fallback_memory(root: Path) -> Any:
    return bootstrap.build_agent_memory_service(
        config=_config(),
        agent_id="main",
        memory_root=root / "memory",
        logger=logging.getLogger("test.bootstrap.memory_owner"),
        home_root=root,
        data_root=root / "data",
        storage_path=root / "state" / "openminion.db",
    )


def _gateway(root: Path, storage: SimpleNamespace, agent_memory: Any = None) -> Any:
    return bootstrap.build_gateway_service(
        agent_service=_ReplyAgent(),
        profile_name="main",
        config=_config(),
        channels=ChannelRegistry([]),
        sessions=storage.sessions,
        idempotency=storage.idempotency,
        security_policy=None,
        channel_authenticity_policy=None,
        config_path=root / "agents.json",
        storage_path=storage.path,
        memory_root=root / "memory",
        home_root=root,
        data_root=root / "data",
        logger=logging.getLogger("test.bootstrap.memory_owner"),
        agent_memory=agent_memory,
    )


def _turn(gateway: Any) -> None:
    response = asyncio.run(
        gateway.run_once(
            channel="console",
            target="local-user",
            message="Remember: the probe colour is green.",
            session_id="s1",
            deliver=False,
        )
    )
    assert response.body == "Noted."


def test_gateway_service_closes_the_memory_it_builds_once(
    tmp_path: Path, created: _Created, runtime_storage: SimpleNamespace
) -> None:
    gateway = _gateway(tmp_path, runtime_storage)
    _turn(gateway)
    open_after_turn = created.sinks_open()
    close_counts = []
    for _ in range(2):
        gateway.close()
        close_counts.append(created.close_counts())

    assert [type(store) for store in created.stores] == [SQLiteMemoryStore]
    assert open_after_turn == [True]
    assert close_counts == [[1, 1, 1], [1, 1, 1]]
    assert created.sinks_open() == [False]
    with pytest.raises(sqlite3.ProgrammingError):
        created.assemblies[0].artifactctl.ingest_bytes(b"after close")


def test_gateway_service_leaves_an_injected_agent_memory_open(
    tmp_path: Path, created: _Created, runtime_storage: SimpleNamespace
) -> None:
    memory = _fallback_memory(tmp_path)
    gateway = _gateway(tmp_path, runtime_storage, agent_memory=memory)
    _turn(gateway)
    gateway_counts = []
    for _ in range(2):
        gateway.close()
        gateway_counts.append(created.close_counts())
    open_after_gateway_close = created.sinks_open()
    owner_counts = []
    for _ in range(2):
        memory.close()
        owner_counts.append(created.close_counts())

    assert len(created.assemblies) == 1
    assert gateway_counts == [[0, 0, 0], [0, 0, 0]]
    assert open_after_gateway_close == [True]
    assert owner_counts == [[1, 1, 1], [1, 1, 1]]


def test_build_agent_memory_service_result_closes_its_assembly_once(
    tmp_path: Path, created: _Created
) -> None:
    memory = _fallback_memory(tmp_path)
    memory.record_turn(**_TURN)
    open_after_turn = created.sinks_open()
    close_counts = []
    for _ in range(2):
        memory.close()
        close_counts.append(created.close_counts())

    assert [type(store) for store in created.stores] == [SQLiteMemoryStore]
    assert open_after_turn == [True]
    assert close_counts == [[1, 1, 1], [1, 1, 1]]
    assert created.sinks_open() == [False]
    with pytest.raises(sqlite3.ProgrammingError):
        created.assemblies[0].artifactctl.ingest_bytes(b"after close")


@pytest.mark.parametrize("owner", ["gateway", "caller"])
def test_fallback_memory_close_waits_for_the_scheduler(
    owner: str,
    tmp_path: Path,
    created: _Created,
    runtime_storage: SimpleNamespace,
) -> None:
    created.scheduler = _Scheduler()
    if owner == "gateway":
        closer = _gateway(tmp_path, runtime_storage)
    else:
        closer = _fallback_memory(tmp_path)
    closer.close()
    blocked_counts = created.close_counts()
    created.scheduler.alive = False
    close_counts = []
    for _ in range(2):
        closer.close()
        close_counts.append(created.close_counts())

    assert blocked_counts == [0, 0, 0]
    assert close_counts == [[1, 1, 1], [1, 1, 1]]


def test_sdk_runtime_memory_gateway_closes_nothing_itself(
    tmp_path: Path, created: _Created
) -> None:
    root = tmp_path / "root"
    config = _config()
    config.runtime.process_mode = "single-process"
    config.runtime.daemon_auto_start = False
    config.storage.path = str(root / ".openminion" / "state.db")
    save_config(config, str(root / "agents.json"))
    runtime = APIRuntime.from_config_path(
        str(root / "agents.json"), home_root=root, data_root=root / ".openminion"
    )
    try:
        with Agent(runtime=runtime) as agent:
            assert agent.run("hello").run_state == "completed"
        [assembly] = created.assemblies
        assert runtime.gateway._agent_memory is assembly.gateway
        assert getattr(assembly.gateway, "close_owner", None) is None
        getattr(assembly.gateway, "close", lambda: None)()
        runtime.gateway.close()
        borrowed_counts = created.close_counts()
        close_counts = []
        for _ in range(2):
            runtime.close()
            close_counts.append(created.close_counts())
    finally:
        runtime.close()

    assert borrowed_counts == [0, 0, 0]
    assert close_counts == [[1, 1, 1], [1, 1, 1]]
