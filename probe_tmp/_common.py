"""Shared helpers for the installed-package probes (no repo test fixtures).

Every probe imports openminion from the installed wheel only. Run each probe
with `python -I` from an empty directory outside any checkout.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import re
import sqlite3
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Any

import openminion

OVERRIDE_ENV_KEYS = (
    "OPENMINION_HOME",
    "OPENMINION_DATA_ROOT",
    "OPENMINION_GENERATED_ROOT",
    "OPENMINION_CONFIG_PATH",
)


# ---------------------------------------------------------------- sanitizing
def _prefixes() -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for value, token in (
        (sys.prefix, "<VENV>"),
        (os.environ.get("VERIFY_WORK"), "<WORK>"),
        (os.environ.get("RUNNER_TEMP"), "<RUNNER_TEMP>"),
        (tempfile.gettempdir(), "<TMP>"),
        (str(Path.home()), "<USER_HOME>"),
    ):
        if value:
            pairs.append((str(Path(value)), token))
            try:
                pairs.append((str(Path(value).resolve()), token))
            except OSError:
                pass
    # Longest first, so a venv inside the temp dir becomes <VENV>, not <TMP>/...
    return sorted(set(pairs), key=lambda item: len(item[0]), reverse=True)


_PREFIXES: list[tuple[str, str]] | None = None


def sanitize_text(text: str) -> str:
    global _PREFIXES
    if _PREFIXES is None:
        _PREFIXES = _prefixes()
    for raw, token in _PREFIXES:
        for variant in {raw, raw.replace("\\", "/"), raw.replace("\\", "\\\\")}:
            text = re.sub(re.escape(variant), lambda _m, t=token: t, text, flags=re.IGNORECASE)
    return text


def sanitize(value: Any) -> Any:
    if isinstance(value, str):
        return sanitize_text(value)
    if isinstance(value, Path):
        return sanitize_text(str(value))
    if isinstance(value, dict):
        return {sanitize(k): sanitize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize(v) for v in value]
    return value


# ---------------------------------------------------------------- reporting
def banner(name: str, **extra: Any) -> dict[str, Any]:
    info = {
        "probe": name,
        "openminion.__file__": openminion.__file__,
        "openminion.__version__": getattr(openminion, "__version__", None),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "sqlite3.sqlite_version": sqlite3.sqlite_version,
        "cwd": os.getcwd(),
        "imported_from_site_packages": "site-packages" in openminion.__file__.replace("\\", "/"),
        "isolated_mode": bool(sys.flags.isolated),
        **extra,
    }
    info = sanitize(info)
    print(json.dumps(info, indent=2))
    return info


def quiet() -> None:
    logging.disable(logging.CRITICAL)


def clear_override_env() -> None:
    for key in OVERRIDE_ENV_KEYS:
        os.environ.pop(key, None)


class Checks:
    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []

    def check(self, name: str, ok: bool, observed: Any = None, expected: Any = None) -> bool:
        item: dict[str, Any] = {"check": name, "pass": bool(ok)}
        if observed is not None:
            item["observed"] = sanitize(observed)
        if expected is not None:
            item["expected"] = sanitize(expected)
        self.items.append(item)
        shown = "" if observed is None else f" | observed={sanitize(observed)!r}"
        print(("PASS " if ok else "FAIL ") + name + shown, flush=True)
        return bool(ok)

    @property
    def all_pass(self) -> bool:
        return all(item["pass"] for item in self.items)

    def summary(self) -> dict[str, int]:
        passed = sum(1 for item in self.items if item["pass"])
        return {"passed": passed, "failed": len(self.items) - passed, "total": len(self.items)}


def write_result(out: Path, payload: dict[str, Any]) -> None:
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(sanitize(payload), indent=2, default=str) + "\n", encoding="utf-8")


def outcome(action) -> str:
    try:
        result = action()
        return "ok" if result is None else f"ok: {result!r}"
    except Exception as exc:  # noqa: BLE001 - probes report it
        return f"{type(exc).__name__}: {exc}"


# ---------------------------------------------------------------- runtimes
def echo_config(
    root: Path,
    *,
    memory_enabled: bool | None = None,
    memory_provider: str | None = None,
) -> Path:
    """The upstream `_echo_runtime` config, without repo fixtures: built-in echo
    provider, single process, explicit storage path under the runtime root."""
    from openminion.base.config import AgentProfileConfig, OpenMinionConfig, save_config

    config = OpenMinionConfig()
    config.agents = {
        "openminion": AgentProfileConfig(name="openminion", provider="echo", default_channel="console")
    }
    config.default_agent = "openminion"
    config.runtime.process_mode = "single-process"
    config.runtime.daemon_auto_start = False
    if memory_enabled is not None:
        config.runtime.memory_enabled = memory_enabled
    if memory_provider is not None:
        config.runtime.memory_provider = memory_provider
    config.storage.path = str(root / ".openminion" / "state.db")
    root.mkdir(parents=True, exist_ok=True)
    path = root / "agents.json"
    save_config(config, str(path))
    return path


def echo_runtime(root: Path, **config_kwargs: Any):
    """Same construction as upstream tests/api/test_artifact_runtime_roots.py::_echo_runtime."""
    from openminion import APIRuntime

    path = echo_config(root, **config_kwargs)
    return APIRuntime.from_config_path(str(path), home_root=root, data_root=root / ".openminion")


# ---------------------------------------------------------------- sqlite tracking
class SqliteTracker:
    """Wraps sqlite3.connect to record every connection opened while active.

    strong=True keeps a strong reference to each connection, so "is it closed"
    is answered deterministically (a closed connection raises ProgrammingError);
    nothing can be freed behind the probe's back. strong=False keeps only ids and
    finds live connections through gc.get_objects(), like the original reports.
    """

    def __init__(self, *, strong: bool = True) -> None:
        self.strong = strong
        self.records: list[dict[str, Any]] = []
        self._conns: list[sqlite3.Connection] = []
        self._orig = sqlite3.connect
        self._orig_dbapi = sqlite3.dbapi2.connect

    def __enter__(self) -> "SqliteTracker":
        orig = self._orig

        def tracking_connect(*args: Any, **kwargs: Any):
            conn = orig(*args, **kwargs)
            frames = [f for f in traceback.extract_stack()[:-1] if "openminion" in f.filename.replace("\\", "/")]
            site = " <- ".join(
                f"{Path(f.filename).name}:{f.lineno} {f.name}" for f in reversed(frames[-3:])
            ) or "(probe code)"
            target = args[0] if args else kwargs.get("database")
            self.records.append({"id": id(conn), "site": site, "target": str(target)})
            if self.strong:
                self._conns.append(conn)
            return conn

        sqlite3.connect = tracking_connect  # type: ignore[assignment]
        sqlite3.dbapi2.connect = tracking_connect  # type: ignore[assignment]
        return self

    def __exit__(self, *exc: Any) -> None:
        sqlite3.connect = self._orig  # type: ignore[assignment]
        sqlite3.dbapi2.connect = self._orig_dbapi  # type: ignore[assignment]

    @staticmethod
    def is_closed(conn: sqlite3.Connection) -> bool:
        try:
            conn.execute("SELECT 1")
            return False
        except sqlite3.ProgrammingError:
            return True

    def status(self, *, owner_sites: tuple[str, ...] | None = None) -> list[dict[str, Any]]:
        out = []
        for record, conn in zip(self.records, self._conns):
            if owner_sites is not None and not any(s in record["site"] for s in owner_sites):
                continue
            out.append({"site": record["site"], "target": sanitize_text(record["target"]), "closed": self.is_closed(conn)})
        return out

    def drop(self) -> None:
        """Close and drop the strong references (cleanup after the checks)."""
        for conn in self._conns:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
        self._conns.clear()


def live_connections_to(path: Path) -> int:
    """Count live, open sqlite3.Connection objects attached to `path` (gc.get_objects)."""
    import gc

    count = 0
    target = os.path.normcase(str(Path(path)))
    for obj in gc.get_objects():
        if type(obj) is sqlite3.Connection:
            try:
                names = [os.path.normcase(r[2]) for r in obj.execute("PRAGMA database_list")]
            except sqlite3.ProgrammingError:
                continue
            if target in names:
                count += 1
    return count
