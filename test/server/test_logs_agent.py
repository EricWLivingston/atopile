"""Tests for the agent run-log source in the logs WebSocket route.

The agent's own run log lives in ``agent_events`` (agent_logs.db), a separate
source from build/test logs. These tests cover the server-side data path the
``/ws/logs`` agent branch relies on: latest-session resolution, level-filtered
chunk fetch, and the row -> shared-entry mapping that lets the existing log
viewer render agent events.
"""

import asyncio
import sqlite3

from atopile.dataclasses import AgentEventRow, Log


def _seed(db_path, rows: list[AgentEventRow]) -> None:
    """Create the agent_events table and insert rows at a known db path."""
    import atopile.model.sqlite as sq

    sq.AgentLogs.init_db()
    sq.AgentLogs.append_chunk(rows)
    # Sanity: the rows landed where we expect.
    assert db_path.exists()


def _row(session_id: str, event: str, **kw) -> AgentEventRow:
    return AgentEventRow(
        session_id=session_id,
        timestamp=kw.pop("timestamp", "2026-06-10T00:00:00+00:00"),
        event=event,
        **kw,
    )


def test_latest_session_id_returns_most_recent(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "agent_logs.db"
    monkeypatch.setattr("atopile.model.sqlite.AGENT_LOGS_DB", db_path)
    import atopile.model.sqlite as sq

    assert sq.AgentLogs.latest_session_id() is None  # no db yet

    _seed(
        db_path,
        [
            _row("sess_old", "session_created"),
            _row("sess_old", "run_progress", phase="thinking", summary="Planning"),
            _row("sess_new", "session_created"),
        ],
    )

    # Most recently inserted row wins.
    assert sq.AgentLogs.latest_session_id() == "sess_new"


def test_fetch_chunk_filters_by_level_and_session(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "agent_logs.db"
    monkeypatch.setattr("atopile.model.sqlite.AGENT_LOGS_DB", db_path)
    import atopile.model.sqlite as sq

    _seed(
        db_path,
        [
            _row("s1", "session_created", level="INFO"),
            _row("s1", "run_failed", level="ERROR", summary="No API key configured"),
            _row("s1", "debug_noise", level="DEBUG", summary="noisy"),
            _row("s2", "session_created", level="INFO"),
        ],
    )

    rows, _ = sq.AgentLogs.fetch_chunk(
        "s1", levels=["INFO", "ERROR"], after_id=0, count=100, order="ASC"
    )
    events = [r["event"] for r in rows]
    assert events == ["session_created", "run_failed"]  # DEBUG filtered, s2 excluded


def test_agent_row_to_entry_maps_onto_shared_shape() -> None:
    row = {
        "id": 42,
        "session_id": "s1",
        "run_id": "r1",
        "timestamp": "2026-06-10T00:00:00+00:00",
        "event": "run_progress",
        "level": "INFO",
        "phase": "tool_start",
        "tool_name": "pyspice_run",
        "summary": "Running simulation",
        "payload": {"k": "v"},
    }
    entry = Log.agent_row_to_entry(row)

    assert entry.id == 42
    assert entry.message == "Running simulation"  # prefers summary
    assert entry.level == "INFO"
    assert entry.audience == str(Log.Audience.AGENT)
    assert entry.logger_name == "agent.run_progress"
    assert entry.stage == "pyspice_run"  # tool surfaces in the stage column
    assert entry.event == "run_progress"
    assert entry.run_id == "r1"
    assert entry.objects == {"k": "v"}


def test_agent_row_to_entry_falls_back_to_event_name() -> None:
    # No summary, no tool: message falls back to event, stage to phase.
    entry = Log.agent_row_to_entry(
        {"id": 1, "event": "session_created", "level": "INFO", "phase": "init"}
    )
    assert entry.message == "session_created"
    assert entry.stage == "init"
    assert entry.logger_name == "agent.session_created"


def test_agent_result_serializes_with_session_id(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "agent_logs.db"
    monkeypatch.setattr("atopile.model.sqlite.AGENT_LOGS_DB", db_path)
    import atopile.model.sqlite as sq

    _seed(
        db_path,
        [
            _row("s1", "session_created", level="INFO"),
            _row("s1", "run_progress", level="INFO", summary="Planning"),
        ],
    )

    rows, _ = sq.AgentLogs.fetch_chunk("s1", after_id=0, count=100, order="ASC")
    entries = [Log.agent_row_to_entry(r) for r in rows]
    payload = Log.AgentResult(logs=entries, session_id="s1").model_dump()

    assert payload["type"] == "agent_logs_result"
    assert payload["session_id"] == "s1"
    assert len(payload["logs"]) == 2
    # Every field the viewer's LogDisplay reads is present.
    first = payload["logs"][0]
    for key in ("timestamp", "level", "logger_name", "message", "stage", "id"):
        assert key in first


class _FakeWS:
    """Records send_json payloads in order; stands in for a WebSocket."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, data: dict) -> None:
        self.sent.append(data)


def _seed_one(db_path, row: AgentEventRow) -> None:
    import atopile.model.sqlite as sq

    sq.AgentLogs.init_db()
    sq.AgentLogs.append_chunk([row])


def test_follow_latest_replaces_then_appends_then_switches(
    monkeypatch, tmp_path
) -> None:
    """Follow-latest mode: first push replaces, same-session pushes append, and a
    brand-new session triggers a replace (auto-switch)."""
    from atopile.server.routes.logs import _push_agent_stream

    db_path = tmp_path / "agent_logs.db"
    monkeypatch.setattr("atopile.model.sqlite.AGENT_LOGS_DB", db_path)

    # Session A with one event.
    _seed_one(db_path, _row("sessA", "session_created", level="INFO"))

    ws = _FakeWS()
    # Follow-latest: no agent_session_id pinned.
    q = Log.AgentStreamQuery.model_validate({"agent": True, "after_id": 0})

    async def run():
        # 1) First push -> replace (agent_logs_result) with session A's batch.
        last_id = await _push_agent_stream(ws, q, 0)
        assert ws.sent[-1]["type"] == "agent_logs_result"
        assert ws.sent[-1]["session_id"] == "sessA"
        assert [e["event"] for e in ws.sent[-1]["logs"]] == ["session_created"]

        # 2) Same session gains an event -> append (stream), only the new row.
        _seed_one(db_path, _row("sessA", "run_progress", level="INFO", summary="Plan"))
        last_id = await _push_agent_stream(ws, q, last_id)
        assert ws.sent[-1]["type"] == "agent_logs_stream"
        assert [e["event"] for e in ws.sent[-1]["logs"]] == ["run_progress"]

        # 3) A brand-new session appears -> auto-switch: replace with session B's batch.
        _seed_one(db_path, _row("sessB", "session_created", level="INFO"))
        last_id = await _push_agent_stream(ws, q, last_id)
        assert ws.sent[-1]["type"] == "agent_logs_result"
        assert ws.sent[-1]["session_id"] == "sessB"
        assert [e["event"] for e in ws.sent[-1]["logs"]] == ["session_created"]

    asyncio.run(run())


def test_pinned_session_does_not_switch(monkeypatch, tmp_path) -> None:
    """A pinned session id keeps streaming that session even when a newer one
    appears (no auto-follow)."""
    from atopile.server.routes.logs import _push_agent_stream

    db_path = tmp_path / "agent_logs.db"
    monkeypatch.setattr("atopile.model.sqlite.AGENT_LOGS_DB", db_path)

    _seed_one(db_path, _row("pinned", "session_created", level="INFO"))

    ws = _FakeWS()
    q = Log.AgentStreamQuery.model_validate(
        {"agent": True, "agent_session_id": "pinned", "after_id": 0}
    )

    async def run():
        last_id = await _push_agent_stream(ws, q, 0)
        assert ws.sent[-1]["session_id"] == "pinned"

        # A newer session appears; pinned stream must ignore it.
        _seed_one(db_path, _row("newer", "session_created", level="INFO"))
        _seed_one(db_path, _row("pinned", "run_progress", level="INFO", summary="x"))
        await _push_agent_stream(ws, q, last_id)
        assert ws.sent[-1]["session_id"] == "pinned"
        assert ws.sent[-1]["type"] == "agent_logs_stream"
        assert [e["event"] for e in ws.sent[-1]["logs"]] == ["run_progress"]

    asyncio.run(run())


def test_init_db_idempotent_and_readable(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "agent_logs.db"
    monkeypatch.setattr("atopile.model.sqlite.AGENT_LOGS_DB", db_path)
    import atopile.model.sqlite as sq

    _seed(db_path, [_row("s1", "session_created")])
    sq.AgentLogs.init_db()  # second call must not raise

    with sqlite3.connect(db_path) as conn:
        n = conn.execute("SELECT COUNT(*) FROM agent_events").fetchone()[0]
    assert n == 1
