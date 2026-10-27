from __future__ import annotations

import logging
from pathlib import Path

import pytest
from sophiagraph.storage import SophiaGraphSqliteStore

from openminion.base.config import OpenMinionConfig
from openminion.modules.brain.adapters.memory.runtime import MemctlAdapter
from openminion.modules.memory.backends import BackendMemoryStoreAdapter
from openminion.modules.memory.errors import InvalidArgumentError
from openminion.modules.memory.backends.external import (
    REFERENCE_SQLITE_ADAPTER_NAME,
    register_external_backend,
    register_reference_sqlite_backend,
)
from openminion.modules.memory.smoke import EphemeralMemorySmokeProvider
from openminion.modules.memory.storage import SQLiteMemoryAuditSink
from openminion.modules.memory.storage import factory as store_factory
from openminion.services.agent.memory.gateway_adapter import (
    DisabledMemoryGatewayAdapter,
)
from openminion.services.runtime.memory import (
    RuntimeMemoryAssembly,
    active_runtime_memory_assembly,
    build_memory_v2_runtime_assembly,
    build_runtime_memory_assembly,
)


class _Service:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def close(self) -> None:
        self.events.append("service.close")


class _ArtifactStore:
    def __init__(self, events: list[str] | None = None) -> None:
        self.events = [] if events is None else events

    def close(self) -> None:
        self.events.append("artifactctl.close")


class _FailingArtifactStore(_ArtifactStore):
    def close(self) -> None:
        super().close()
        raise OSError("artifact index locked")


def _unavailable_artifact_store() -> None:
    raise OSError("artifact root unavailable")


def _memory_v2_assembly(tmp_path, **overrides) -> RuntimeMemoryAssembly:
    config = OpenMinionConfig()
    config.runtime.memory_enabled = True
    config.runtime.memory_provider = "memory_v2"
    arguments = {
        "config": config,
        "agent_id": "alpha",
        "memory_root": tmp_path,
        "logger": logging.getLogger("test.memory.artifact"),
        "config_manager": None,
        "home_root": None,
        "data_root": None,
        "session_context": None,
        "retrieve_ctl": None,
        "storage_path": None,
    }
    return build_memory_v2_runtime_assembly(**{**arguments, **overrides})


class _Scheduler:
    def __init__(self, events: list[str], *, live_after_stop: bool = False) -> None:
        self.events = events
        self.live = live_after_stop
        self.record_source = None

    def bind_record_source(self, record_source) -> None:
        self.events.append("scheduler.bind")
        self.record_source = record_source

    def start(self) -> None:
        self.events.append("scheduler.start")

    def stop(self) -> None:
        self.events.append("scheduler.stop")

    def is_alive(self) -> bool:
        return self.live


class _SnapshotVectorAdapter:
    def __init__(self) -> None:
        self.source = None
        self.snapshot = None

    def bind_record_source(self, source) -> None:
        self.source = source

    def sync_pending_records(self) -> int:
        self.snapshot = self.source.vector_sync_snapshot()
        return len(self.snapshot["current"])


class _BindingScheduler(_Scheduler):
    def __init__(self, events: list[str], adapter: _SnapshotVectorAdapter) -> None:
        super().__init__(events)
        self.adapter = adapter

    def bind_record_source(self, record_source) -> None:
        super().bind_record_source(record_source)
        self.adapter.bind_record_source(record_source)


def test_active_assembly_owns_one_non_duplicated_runtime(tmp_path) -> None:
    config = OpenMinionConfig()
    config.runtime.memory_enabled = True
    config.runtime.memory_provider = "memory_v2"

    assembly = build_memory_v2_runtime_assembly(
        config=config,
        agent_id="alpha",
        memory_root=tmp_path,
        logger=logging.getLogger("test.memory.assembly"),
        config_manager=None,
        home_root=None,
        data_root=None,
        session_context=None,
        retrieve_ctl=None,
        storage_path=None,
        artifactctl_factory=lambda: None,
    )

    assert assembly.gateway is not None
    assert assembly.service is not None
    assert isinstance(assembly.memctl, MemctlAdapter)
    assert assembly.memctl._backend is assembly.service
    assert assembly.gateway._service is assembly.service
    assert assembly.close().closed is True


def test_default_artifact_store_uses_runtime_artifact_config(
    tmp_path, monkeypatch, fresh_config_manager
) -> None:
    configs: list[object] = []
    monkeypatch.setattr(
        "openminion.services.runtime.memory.create_default_artifactctl",
        lambda config=None: configs.append(config),
    )
    config = OpenMinionConfig()
    config.runtime.memory_enabled = True
    config.runtime.memory_provider = "memory_v2"

    assembly = build_memory_v2_runtime_assembly(
        config=config,
        agent_id="alpha",
        memory_root=tmp_path,
        logger=logging.getLogger("test.memory.assembly"),
        config_manager=fresh_config_manager,
        home_root=fresh_config_manager.home_root,
        data_root=fresh_config_manager.data_root,
        session_context=None,
        retrieve_ctl=None,
        storage_path=None,
    )
    assembly.close()

    assert configs == [fresh_config_manager.get("artifact")]


@pytest.mark.parametrize("source", ["default", "factory"])
def test_assembly_closes_artifact_store_it_obtains_once(
    tmp_path, monkeypatch, source
) -> None:
    store = _ArtifactStore()
    overrides: dict[str, object] = {}
    if source == "default":
        monkeypatch.setattr(
            "openminion.services.runtime.memory.create_default_artifactctl",
            lambda config=None: store,
        )
    else:
        overrides["artifactctl_factory"] = lambda: store

    assembly = _memory_v2_assembly(tmp_path, **overrides)

    assert assembly.artifactctl is store
    first = assembly.close()
    second = assembly.close()

    assert first is second
    assert first.closed is True
    assert store.events == ["artifactctl.close"]


@pytest.mark.parametrize(
    "factory",
    [lambda: None, _unavailable_artifact_store],
    ids=["returns_none", "raises"],
)
def test_assembly_owns_no_artifact_store_the_factory_did_not_provide(
    tmp_path, factory
) -> None:
    assembly = _memory_v2_assembly(tmp_path, artifactctl_factory=factory)

    assert assembly.artifactctl is None
    assert assembly.close().closed is True


def test_gateway_only_assemblies_obtain_no_artifact_store(
    tmp_path, monkeypatch
) -> None:
    created: list[object] = []
    monkeypatch.setattr(
        "openminion.services.runtime.memory.create_default_artifactctl",
        lambda config=None: created.append(config),
    )
    disabled = OpenMinionConfig()
    disabled.runtime.memory_enabled = False
    smoke = OpenMinionConfig()
    smoke.runtime.memory_provider = "memory_v2_smoke"
    assemblies = [
        build_runtime_memory_assembly(
            config=config,
            agent_id="alpha",
            memory_root=tmp_path,
            logger=logging.getLogger("test.memory.artifact"),
        )
        for config in (disabled, smoke)
    ]
    assemblies.append(
        _memory_v2_assembly(
            tmp_path,
            resolve_runtime_memory_config_fn=lambda **kwargs: {
                "backend": {"provider": "none"}
            },
        )
    )

    for assembly in assemblies:
        assert assembly.artifactctl is None
        assert assembly.close().closed is True
    assert created == []


def test_disabled_assembly_keeps_gateway_without_runtime_owners() -> None:
    for gateway in (
        DisabledMemoryGatewayAdapter(agent_id="alpha"),
        EphemeralMemorySmokeProvider(agent_id="alpha"),
    ):
        assembly = RuntimeMemoryAssembly(gateway=gateway)

        assert assembly.gateway is gateway
        assert assembly.service is None
        assert assembly.memctl is None
        assert assembly.scheduler is None
        assert assembly.close().closed is True


def test_backend_none_builds_gateway_only_assembly(tmp_path) -> None:
    assembly = build_memory_v2_runtime_assembly(
        config=OpenMinionConfig(),
        agent_id="alpha",
        memory_root=tmp_path,
        logger=logging.getLogger("test.memory.none"),
        config_manager=None,
        home_root=None,
        data_root=None,
        session_context=None,
        retrieve_ctl=None,
        storage_path=None,
        resolve_runtime_memory_config_fn=lambda **kwargs: {
            "backend": {"provider": "none"}
        },
    )

    assert isinstance(assembly.gateway, DisabledMemoryGatewayAdapter)
    assert assembly.service is None
    assert assembly.memctl is None
    assert assembly.scheduler is None


def test_assembly_starts_and_closes_scheduler_before_service() -> None:
    events: list[str] = []
    service = _Service(events)
    adapter = _SnapshotVectorAdapter()
    scheduler = _BindingScheduler(events, adapter)
    assembly = active_runtime_memory_assembly(
        gateway=object(),
        service=service,  # type: ignore[arg-type]
        agent_id="alpha",
        vector_adapter=adapter,
        scheduler=scheduler,
    )

    assembly.start()
    first = assembly.close()
    second = assembly.close()

    assert scheduler.record_source is assembly.memctl
    assert events == [
        "scheduler.bind",
        "scheduler.start",
        "scheduler.stop",
        "service.close",
    ]
    assert first is second
    assert first.closed is True


def test_assembly_syncs_complete_snapshot_through_memctl(tmp_path) -> None:
    from openminion.modules.memory.service import MemoryService
    from openminion.modules.memory.storage.sqlite.store import SQLiteMemoryStore

    service = MemoryService(store=SQLiteMemoryStore(tmp_path / "memory.db"))
    service.write_record(
        scope="agent:alpha",
        record_type="fact",
        title="Canonical title",
        content="Canonical content",
    )
    adapter = _SnapshotVectorAdapter()
    scheduler = _BindingScheduler([], adapter)
    assembly = active_runtime_memory_assembly(
        gateway=object(),
        service=service,
        agent_id="alpha",
        vector_adapter=adapter,
        scheduler=scheduler,
    )

    assembly.start()

    assert len(adapter.snapshot["current"]) == 1
    assert adapter.snapshot["current"][0]["text"] == (
        "Canonical title\nCanonical content"
    )
    assembly.close()


def test_assembly_leaves_service_open_while_scheduler_is_live() -> None:
    events: list[str] = []
    service = _Service(events)
    scheduler = _Scheduler(events, live_after_stop=True)
    assembly = active_runtime_memory_assembly(
        gateway=object(),
        service=service,  # type: ignore[arg-type]
        agent_id="alpha",
        scheduler=scheduler,
        artifactctl=_ArtifactStore(events),
    )

    blocked = assembly.close()

    assert blocked.closed is False
    assert blocked.reason_code == "scheduler_still_running"
    assert events == ["scheduler.stop"]

    scheduler.live = False
    assert assembly.close().closed is True
    assert assembly.close().closed is True
    assert events == [
        "scheduler.stop",
        "scheduler.stop",
        "service.close",
        "artifactctl.close",
    ]


def test_artifact_store_close_error_keeps_recorded_memory_close() -> None:
    events: list[str] = []
    assembly = active_runtime_memory_assembly(
        gateway=object(),
        service=_Service(events),  # type: ignore[arg-type]
        agent_id="alpha",
        artifactctl=_FailingArtifactStore(events),
    )

    with pytest.raises(OSError, match="artifact index locked"):
        assembly.close()
    repeated = assembly.close()

    assert repeated.closed is True
    assert repeated.reason_code == "closed"
    assert events == ["service.close", "artifactctl.close"]


@pytest.fixture
def audit_sinks(monkeypatch):
    created: list[SQLiteMemoryAuditSink] = []

    class CountingAuditSink(SQLiteMemoryAuditSink):
        def __init__(self, db_path) -> None:
            super().__init__(db_path)
            self.close_calls = 0
            created.append(self)

        def close(self) -> None:
            self.close_calls += 1
            super().close()

    monkeypatch.setattr(
        "openminion.services.runtime.memory.SQLiteMemoryAuditSink",
        CountingAuditSink,
    )
    yield created
    for sink in created:
        SQLiteMemoryAuditSink.close(sink)


def _write_audited_record(assembly: RuntimeMemoryAssembly) -> None:
    assembly.service.write_record(
        scope="agent:alpha",
        record_type="fact",
        title="Audited",
        content="Audited content",
    )


def test_assembly_closes_the_audit_sink_it_creates_once(tmp_path, audit_sinks) -> None:
    assembly = _memory_v2_assembly(tmp_path, artifactctl_factory=lambda: None)
    _write_audited_record(assembly)
    [sink] = audit_sinks
    assert sink.db_path == tmp_path / "memory.audit.db"
    assert sink._conn is not None

    first = assembly.close()
    second = assembly.close()

    assert first is second
    assert first.closed is True
    assert sink.close_calls == 1
    assert sink._conn is None


def test_audit_sink_stays_open_while_scheduler_is_live(tmp_path, audit_sinks) -> None:
    scheduler = _Scheduler([], live_after_stop=True)
    assembly = _memory_v2_assembly(
        tmp_path, scheduler=scheduler, artifactctl_factory=lambda: None
    )
    _write_audited_record(assembly)
    [sink] = audit_sinks

    blocked = assembly.close()

    assert blocked.reason_code == "scheduler_still_running"
    assert sink.close_calls == 0
    assert sink._conn is not None
    _write_audited_record(assembly)
    assert len(sink.list_events()) == 2

    scheduler.live = False
    assert assembly.close().closed is True
    assert assembly.close().closed is True
    assert sink.close_calls == 1
    assert sink._conn is None


@pytest.fixture
def store_closes(monkeypatch):
    events: list[str] = []

    def counting(store_type):
        class CountingStore(store_type):
            def close(self) -> None:
                events.append("store.close")
                super().close()

        return CountingStore

    for name in ("SQLiteMemoryStore", "InMemoryMemoryStore", "RemoteMemoryStore"):
        monkeypatch.setattr(store_factory, name, counting(getattr(store_factory, name)))
    return events


@pytest.mark.parametrize(
    "memory_config",
    [
        None,
        {"store": {"backend": "mock"}},
        {"store": {"backend": "remote", "remote": {"endpoint": "http://127.0.0.1:9"}}},
    ],
    ids=["sqlite", "mock", "remote"],
)
def test_assembly_closes_the_memory_store_it_creates_once(
    tmp_path, store_closes, memory_config
) -> None:
    overrides: dict[str, object] = {
        "artifactctl_factory": lambda: _ArtifactStore(store_closes)
    }
    if memory_config is not None:
        overrides["resolve_runtime_memory_config_fn"] = lambda **kwargs: memory_config
    assembly = _memory_v2_assembly(tmp_path, **overrides)
    assert assembly.delegated_store.store is assembly.service._store

    first = assembly.close()
    second = assembly.close()

    assert first is second
    assert first.closed is True
    assert store_closes == ["store.close", "artifactctl.close"]


class _ReferencedArtifactStore(_ArtifactStore):
    def ref_add(self, owner_type: str, owner_id: str, target: str) -> None:
        self.events.append("artifactctl.ref_add")


def test_assembly_closes_the_artifact_store_its_sqlite_store_creates_once(
    tmp_path, monkeypatch
) -> None:
    events: list[str] = []
    monkeypatch.setattr(
        "openminion.modules.memory.storage.sqlite.store.create_default_artifactctl",
        lambda config=None: _ReferencedArtifactStore(events),
    )
    assembly = _memory_v2_assembly(
        tmp_path, artifactctl_factory=_unavailable_artifact_store
    )
    assembly.service.write_record(
        scope="agent:alpha",
        record_type="fact",
        title="Referenced",
        content="Referenced content",
        evidence_refs=["a" * 64],
    )

    first = assembly.close()
    second = assembly.close()

    assert first is second
    assert first.closed is True
    assert events == ["artifactctl.ref_add", "artifactctl.close"]


def test_memory_store_stays_open_while_scheduler_is_live(
    tmp_path, store_closes
) -> None:
    scheduler = _Scheduler([], live_after_stop=True)
    assembly = _memory_v2_assembly(
        tmp_path,
        scheduler=scheduler,
        artifactctl_factory=lambda: _ArtifactStore(store_closes),
    )

    blocked = assembly.close()

    assert blocked.reason_code == "scheduler_still_running"
    assert store_closes == []

    scheduler.live = False
    assert assembly.close().closed is True
    assert assembly.close().closed is True
    assert store_closes == ["store.close", "artifactctl.close"]


class _ClosingReferenceBackend(SophiaGraphSqliteStore):
    def __init__(self, db_path: Path, events: list[str]) -> None:
        super().__init__(db_path)
        self.events = events

    def close(self) -> None:
        self.events.append("backend.close")


def _external_assembly(
    tmp_path, events: list[str], *, closes: bool, **overrides
) -> RuntimeMemoryAssembly:
    register_reference_sqlite_backend()
    adapter = REFERENCE_SQLITE_ADAPTER_NAME
    if closes:
        adapter = "closing-reference-sqlite"
        register_external_backend(
            adapter,
            factory=lambda *, config, **_: _ClosingReferenceBackend(
                Path(config.options["db_path"]), events
            ),
        )
    memory_config = {
        "backend": {
            "provider": "external",
            "external_adapter": adapter,
            "options": {"db_path": str(tmp_path / "external.sqlite3")},
        }
    }
    return _memory_v2_assembly(
        tmp_path,
        resolve_runtime_memory_config_fn=lambda **kwargs: memory_config,
        artifactctl_factory=lambda: _ArtifactStore(events),
        **overrides,
    )


@pytest.mark.parametrize("closes", [True, False], ids=["backend_close", "no_close"])
def test_external_assembly_closes_the_backend_it_owns_once(tmp_path, closes) -> None:
    events: list[str] = []
    assembly = _external_assembly(tmp_path, events, closes=closes)
    assert isinstance(assembly.service._store, BackendMemoryStoreAdapter)
    assert assembly.delegated_store is None

    first = assembly.close()
    second = assembly.close()

    assert first is second
    assert first.closed is True
    backend_closes = ["backend.close"] if closes else []
    assert events == [*backend_closes, "artifactctl.close"]


def test_external_backend_stays_open_while_scheduler_is_live(tmp_path) -> None:
    events: list[str] = []
    scheduler = _Scheduler(events, live_after_stop=True)
    assembly = _external_assembly(tmp_path, events, closes=True, scheduler=scheduler)

    blocked = assembly.close()

    assert blocked.reason_code == "scheduler_still_running"
    assert events == ["scheduler.stop"]

    scheduler.live = False
    assert assembly.close().closed is True
    assert assembly.close().closed is True
    assert events == [
        "scheduler.stop",
        "scheduler.stop",
        "backend.close",
        "artifactctl.close",
    ]


@pytest.mark.parametrize("provider", ["sophiagraph", "external"])
def test_assembly_creates_a_memory_store_only_for_the_builtin_backend(
    tmp_path, monkeypatch, audit_sinks, provider
) -> None:
    stores: list[object] = []
    resolve_backend = store_factory.resolve_memory_backend

    def record_backend(**kwargs):
        resolved = resolve_backend(**kwargs)
        stores.append(resolved.store)
        return resolved

    monkeypatch.setattr(
        "openminion.services.runtime.memory.resolve_memory_backend", record_backend
    )
    events: list[str] = []
    if provider == "external":
        assembly = _external_assembly(tmp_path, events, closes=True)
    else:
        assembly = _memory_v2_assembly(
            tmp_path, artifactctl_factory=lambda: _ArtifactStore(events)
        )
    created = 1 if provider == "sophiagraph" else 0

    assert (len(stores), len(audit_sinks)) == (created, created)
    assert assembly.close().closed is True


# A build that raises closes what it obtained, once, and its own error reaches
# the caller (#390). The store_closes fixture records every memory store close.


class _GatewayUnavailable(RuntimeError):
    """A failure after the memory service exists: building the gateway."""


def _failing_gateway(*_args, **_kwargs):
    raise _GatewayUnavailable("gateway unavailable")


class _ClosableVectorAdapter(_SnapshotVectorAdapter):
    def __init__(self, events: list[str]) -> None:
        super().__init__()
        self.events = events

    def close(self) -> None:
        self.events.append("vector.close")


def test_store_resolution_failure_closes_the_artifact_store(
    tmp_path, store_closes, audit_sinks
) -> None:
    memory_config = {"store": {"backend": "remote"}}

    with pytest.raises(InvalidArgumentError, match="requires remote.endpoint"):
        _memory_v2_assembly(
            tmp_path,
            resolve_runtime_memory_config_fn=lambda **kwargs: memory_config,
            artifactctl_factory=lambda: _ArtifactStore(store_closes),
        )

    assert store_closes == ["artifactctl.close"]
    assert audit_sinks == []


def test_audit_sink_failure_closes_the_store_it_created_once(
    tmp_path, monkeypatch, store_closes
) -> None:
    def unavailable_audit_sink(db_path):
        raise OSError("audit database unavailable")

    monkeypatch.setattr(
        "openminion.services.runtime.memory.SQLiteMemoryAuditSink",
        unavailable_audit_sink,
    )

    with pytest.raises(OSError, match="audit database unavailable"):
        _memory_v2_assembly(
            tmp_path, artifactctl_factory=lambda: _ArtifactStore(store_closes)
        )

    assert store_closes == ["store.close", "artifactctl.close"]


@pytest.mark.parametrize(
    "memory_config",
    [
        None,
        {"store": {"backend": "mock"}},
        {"store": {"backend": "remote", "remote": {"endpoint": "http://127.0.0.1:9"}}},
    ],
    ids=["sqlite", "mock", "remote"],
)
def test_later_failure_closes_the_store_through_the_service_once(
    tmp_path, store_closes, audit_sinks, memory_config
) -> None:
    overrides: dict[str, object] = {
        "artifactctl_factory": lambda: _ArtifactStore(store_closes),
        "adapter_cls": _failing_gateway,
    }
    if memory_config is not None:
        overrides["resolve_runtime_memory_config_fn"] = lambda **kwargs: memory_config

    with pytest.raises(_GatewayUnavailable, match="gateway unavailable"):
        _memory_v2_assembly(tmp_path, **overrides)

    assert store_closes == ["store.close", "artifactctl.close"]
    [sink] = audit_sinks
    assert sink.close_calls == 1


def test_unregistered_external_adapter_closes_the_artifact_store(
    tmp_path, store_closes, audit_sinks
) -> None:
    memory_config = {
        "backend": {"provider": "external", "external_adapter": "not_registered"}
    }

    with pytest.raises(InvalidArgumentError, match="No external backend registered"):
        _memory_v2_assembly(
            tmp_path,
            resolve_runtime_memory_config_fn=lambda **kwargs: memory_config,
            artifactctl_factory=lambda: _ArtifactStore(store_closes),
        )

    assert store_closes == ["artifactctl.close"]
    assert audit_sinks == []


def test_failed_build_keeps_its_error_when_a_close_fails(
    tmp_path, store_closes
) -> None:
    with pytest.raises(_GatewayUnavailable, match="gateway unavailable"):
        _memory_v2_assembly(
            tmp_path,
            artifactctl_factory=lambda: _FailingArtifactStore(store_closes),
            adapter_cls=_failing_gateway,
        )

    assert store_closes == ["store.close", "artifactctl.close"]


def test_failed_build_leaves_borrowed_runtime_objects_open(
    tmp_path, store_closes
) -> None:
    scheduler = _Scheduler(store_closes)
    vector_adapter = _ClosableVectorAdapter(store_closes)

    with pytest.raises(_GatewayUnavailable, match="gateway unavailable"):
        _memory_v2_assembly(
            tmp_path,
            scheduler=scheduler,
            vector_adapter=vector_adapter,
            artifactctl_factory=lambda: _ArtifactStore(store_closes),
            adapter_cls=_failing_gateway,
        )

    # The caller's scheduler and vector adapter are neither stopped nor closed.
    assert store_closes == ["store.close", "artifactctl.close"]


def test_successful_build_hands_what_it_obtained_to_the_assembly(
    tmp_path, store_closes, audit_sinks
) -> None:
    assembly = _memory_v2_assembly(
        tmp_path, artifactctl_factory=lambda: _ArtifactStore(store_closes)
    )
    [sink] = audit_sinks
    assert store_closes == []
    assert sink.close_calls == 0

    assert assembly.close().closed is True
    assert store_closes == ["store.close", "artifactctl.close"]
    assert sink.close_calls == 1
