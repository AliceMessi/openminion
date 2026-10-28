"""Room active agent: a selection racing a leave of the agent it selects.

`set_active_agent` (modules/storage/runtime/session_store/participants.py)
checks that the agent is an active participant (`get_participant`), then
writes `active_agent_id` with an unconditional UPDATE. The check and the write
are separate statements, outside any transaction, and do not take the
session-row lock that joins and leaves take (`_lock_session_row`, #385).

The probe opens three runtime storages on one database: A selects, B leaves,
M sets up the rooms and reads the results. Each case uses a new room with a
human owner and the agents `alpha` and `beta`; `alpha` joined first and is the
active agent.

Held cases (one call is held, the other runs on the other storage):
1. A's `set_active_agent(beta)` is held when it sends its write
   (`UPDATE sessions SET active_agent_id = ...`), after its participant check
   passed. B's `remove_participant(beta)` runs. Then the selection goes on.
2. B's `remove_participant(beta)` is held in its open transaction, after it
   marked `beta` as left and before its fallback (when it reads the active
   agent, as tests/_room_participant_scenarios.py
   check_leave_and_rejoin_wait_for_each_other does). A's
   `set_active_agent(beta)` runs. Then the leave goes on.
A hold ends when the other call returns, or after HOLD_SECONDS (below the 5 s
SQLite busy timeout of the runtime storages), whichever comes first. The held
call itself is not changed: the wrapper only waits, then calls the real code.

Controls (one call after another on the two storages, no hold):
3. A selects `beta`, no leave.
4. `beta` is selected, then B removes `beta`, no selection running.
5. B removes `beta`, then A selects `beta`.

Expected in every case: after both calls return, the stored active agent is
an agent that is still listed as a participant. In a held case the selection
is either refused or applies before the leave, whose fallback then runs. The
controls also have exact expected values. Each case prints OK or FAIL; the
exit status is 1 when any case fails.

SQLite always; PostgreSQL too when OPENMINION_TEST_POSTGRES_URL is set (needs
the postgres extra). From an openminion checkout:
PYTHONPATH=src python probe_selection_leave_race.py
or with an installed openminion from any directory.
"""

import os
import sqlite3
import sys
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote_plus

import openminion
from openminion.modules.storage.runtime.context import build_runtime_storage

HOLD_SECONDS = 1.0
TIMEOUT = 20
SELECTION_WRITE = "UPDATE sessions SET active_agent_id"
FAILURES = []
BACKEND = ["?"]


def call_in_thread(target):
    box = {"done": threading.Event()}

    def run():
        try:
            box["result"] = target()
        except BaseException as exc:  # reported, not raised
            box["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            box["done"].set()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, box


def outcome(thread, box):
    thread.join(TIMEOUT)
    if thread.is_alive():
        return "still running after the timeout (blocked)"
    if "error" in box:
        return box["error"]
    return f"returned {box['result']!r}"


def new_room(main, name):
    sessions = main.sessions
    room = sessions.create_room(channel="cli", target=f"{name}-{uuid.uuid4().hex[:8]}")
    sessions.add_participant(
        session_id=room.id, participant_type="human", participant_id="owner", role="owner"
    )
    for agent in ("alpha", "beta"):
        sessions.add_participant(
            session_id=room.id, participant_type="agent", participant_id=agent
        )
    return room.id


def state(main, room_id):
    sessions = main.sessions
    agents = [
        item.participant_id
        for item in sessions.list_participants(room_id)
        if item.participant_type == "agent"
    ]
    stored = sessions.get_session(room_id).active_agent_id
    return agents, stored, sessions.get_active_agent(room_id)


def report(name, main, room_id, expected=None):
    agents, stored, active = state(main, room_id)
    print(
        f"   after: agents listed {agents} | stored active_agent_id {stored!r} | "
        f"get_active_agent {active!r}"
    )
    problems = []
    if stored is not None and stored not in agents:
        problems.append(f"stored active agent {stored!r} is not a listed agent")
    if expected is not None and (agents, stored) != expected:
        problems.append(f"expected agents {expected[0]} and stored {expected[1]!r}")
    if problems:
        FAILURES.append(f"{BACKEND[0]} case {name}")
        print(f"   FAIL: {'; '.join(problems)}")
    else:
        print("   OK")


def select(storage, room_id, agent):
    return lambda: storage.sessions.set_active_agent(
        session_id=room_id, agent_id=agent
    ).active_agent_id


def leave(storage, room_id, agent):
    return lambda: storage.sessions.remove_participant(
        session_id=room_id, participant_type="agent", participant_id=agent
    )


def selection_held_before_its_write(main, a, b):
    """Case 1: A's selection of beta is held when it sends its write."""
    room_id = new_room(main, "selection-held")
    backend = a.sessions._participants._backend
    real_execute_count = backend.execute_count
    reached, release = threading.Event(), threading.Event()

    def execute_count(sql, params=None):
        if sql.lstrip().startswith(SELECTION_WRITE) and not reached.is_set():
            reached.set()
            release.wait(TIMEOUT)
        return real_execute_count(sql, params)

    backend.execute_count = execute_count
    try:
        selection = call_in_thread(select(a, room_id, "beta"))
        if not reached.wait(TIMEOUT):
            print(f"   A: set_active_agent(beta) never sent its write: {outcome(*selection)}")
            FAILURES.append(f"{BACKEND[0]} case 1")
            return
        print("   A: set_active_agent(beta) passed its participant check, held before its write")
        removal = call_in_thread(leave(b, room_id, "beta"))
        finished = removal[1]["done"].wait(HOLD_SECONDS)
        print(f"   B: remove_participant(beta) returned during the hold: {finished}")
        release.set()
        print(f"   A: set_active_agent(beta) {outcome(*selection)}")
        print(f"   B: remove_participant(beta) {outcome(*removal)}")
    finally:
        release.set()
        del backend.execute_count
    report("1", main, room_id)


def leave_held_before_its_fallback(main, a, b):
    """Case 2: B's leave of beta is held in its transaction, before its fallback."""
    room_id = new_room(main, "leave-held")
    owner = b.sessions._participants
    real_get_active_agent = owner.get_active_agent
    reached, release = threading.Event(), threading.Event()

    def get_active_agent(session_id):
        if not reached.is_set():
            reached.set()
            release.wait(TIMEOUT)
        return real_get_active_agent(session_id)

    a_backend = a.sessions._participants._backend
    real_execute_count = a_backend.execute_count
    write_sent_during_hold = []

    def noting_execute_count(sql, params=None):
        if sql.lstrip().startswith(SELECTION_WRITE) and not write_sent_during_hold:
            write_sent_during_hold.append(not release.is_set())
        return real_execute_count(sql, params)

    owner.get_active_agent = get_active_agent
    a_backend.execute_count = noting_execute_count
    try:
        removal = call_in_thread(leave(b, room_id, "beta"))
        if not reached.wait(TIMEOUT):
            print(f"   B: remove_participant(beta) never reached its fallback: {outcome(*removal)}")
            FAILURES.append(f"{BACKEND[0]} case 2")
            return
        print("   B: remove_participant(beta) marked beta as left, held before its fallback")
        selection = call_in_thread(select(a, room_id, "beta"))
        finished = selection[1]["done"].wait(HOLD_SECONDS)
        print(f"   A: set_active_agent(beta) returned during the hold: {finished}")
        print(
            "   A: set_active_agent(beta) sent its write during the hold: "
            f"{bool(write_sent_during_hold and write_sent_during_hold[0])}"
        )
        release.set()
        print(f"   B: remove_participant(beta) {outcome(*removal)}")
        print(f"   A: set_active_agent(beta) {outcome(*selection)}")
    finally:
        release.set()
        del owner.get_active_agent
        del a_backend.execute_count
    report("2", main, room_id)


def control_selection(main, a, b):
    room_id = new_room(main, "control-selection")
    print(f"   A: set_active_agent(beta) {outcome(*call_in_thread(select(a, room_id, 'beta')))}")
    report("3", main, room_id, expected=(["alpha", "beta"], "beta"))


def control_leave(main, a, b):
    room_id = new_room(main, "control-leave")
    main.sessions.set_active_agent(session_id=room_id, agent_id="beta")
    print("   M: set_active_agent(beta) before the leave")
    print(f"   B: remove_participant(beta) {outcome(*call_in_thread(leave(b, room_id, 'beta')))}")
    report("4", main, room_id, expected=(["alpha"], "alpha"))


def control_selection_after_leave(main, a, b):
    room_id = new_room(main, "control-after-leave")
    print(f"   B: remove_participant(beta) {outcome(*call_in_thread(leave(b, room_id, 'beta')))}")
    print(f"   A: set_active_agent(beta) {outcome(*call_in_thread(select(a, room_id, 'beta')))}")
    report("5", main, room_id, expected=(["alpha"], "alpha"))


CASES = (
    ("1. selection held before its write, leave on the other storage", selection_held_before_its_write),
    ("2. leave held before its fallback, selection on the other storage", leave_held_before_its_fallback),
    ("3. control: selection, no leave", control_selection),
    ("4. control: leave of the active agent, no selection", control_leave),
    ("5. control: selection after the leave", control_selection_after_leave),
)


def run_backend(label, open_storage):
    print(f"== {label}")
    BACKEND[0] = label.split()[0]
    main, a, b = open_storage(), open_storage(), open_storage()
    try:
        for title, case in CASES:
            print(title)
            started = time.monotonic()
            case(main, a, b)
            print(f"   ({time.monotonic() - started:.1f} s)")
    finally:
        for storage in (main, a, b):
            storage.close()


@contextmanager
def postgres_url():
    import sqlalchemy as sa

    base = os.environ["OPENMINION_TEST_POSTGRES_URL"].strip()
    schema = f"probe_selection_leave_{uuid.uuid4().hex[:12]}"
    admin = sa.create_engine(base, future=True)
    with admin.begin() as conn:
        version = conn.execute(sa.text("SHOW server_version")).scalar()
        conn.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
    sep = "&" if "?" in base else "?"
    try:
        yield f"{base}{sep}options={quote_plus(f'-csearch_path={schema}')}", version
    finally:
        with admin.begin() as conn:
            conn.execute(sa.text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin.dispose()


def main():
    print(
        f"openminion {getattr(openminion, '__version__', '?')} from "
        f"{Path(openminion.__file__).parent.parent}"
    )
    print(f"python {sys.version.split()[0]} {sys.platform} | SQLite {sqlite3.sqlite_version}")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        path = Path(tmp) / "state" / "openminion.db"
        run_backend(
            f"SQLite {sqlite3.sqlite_version}",
            lambda: build_runtime_storage(path, env={}),
        )
        if os.getenv("OPENMINION_TEST_POSTGRES_URL", "").strip():
            with postgres_url() as (url, version):
                run_backend(
                    f"PostgreSQL {version}",
                    lambda: build_runtime_storage(
                        Path(tmp) / "pg" / f"{uuid.uuid4().hex}.db",
                        env={},
                        record_backend="record.postgres",
                        record_backend_options={"url": url},
                    ),
                )
        else:
            print("== PostgreSQL: skipped (OPENMINION_TEST_POSTGRES_URL not set)")
    print(f"SUMMARY failed cases: {FAILURES or 'none'}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
