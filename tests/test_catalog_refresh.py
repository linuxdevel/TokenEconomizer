"""The tool catalog refreshes itself: a Claude Code session start probes it again, detached, when
it is missing, older than a day, or recorded for another `claude` than the one installed now."""
import json
import os
import types

import pytest

from tokeneconomizer.plugin import adapter, toolcheck
from tests.test_live_hooks import call, env, repo  # noqa: F401

# Taken before the suite's autouse fixture stubs the module attribute for every other test.
maybe_refresh = toolcheck.maybe_refresh_catalog


@pytest.fixture
def claude(tmp_path, monkeypatch):
    exe = tmp_path / "bin" / "claude"
    exe.parent.mkdir()
    exe.write_text("v1")
    monkeypatch.setattr(toolcheck, "claude_cli", lambda: str(exe))
    return exe


def _ctx(tmp_path, host="claude"):
    spawned = []
    ctx = types.SimpleNamespace(host=types.SimpleNamespace(name=host),
                                root=str(tmp_path / "state"),
                                spawn=lambda argv, log: spawned.append((argv, log)))
    return ctx, spawned


def _record(ctx, fp, probed_at):
    path = adapter.catalog_path(ctx.root, "claude")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"tools": ["Bash", "Read"], "cli_fingerprint": fp, "probed_at": probed_at}, fh)


def test_a_missing_catalog_starts_one_detached_probe(tmp_path, claude):
    ctx, spawned = _ctx(tmp_path)
    assert maybe_refresh(ctx, now=1000.0) == "missing"
    assert spawned == [(["toolprune", "refresh-catalog"], "catalog.log")]
    # A second session starting before the probe wrote anything does not start another.
    assert maybe_refresh(ctx, now=1010.0) is None
    assert len(spawned) == 1


def test_a_failed_probe_is_retried_after_an_hour(tmp_path, claude):
    ctx, spawned = _ctx(tmp_path)
    maybe_refresh(ctx, now=1000.0)
    assert maybe_refresh(ctx, now=1000.0 + toolcheck.CATALOG_RETRY + 1) == "missing"
    assert len(spawned) == 2


def test_a_current_catalog_is_left_alone(tmp_path, claude):
    ctx, spawned = _ctx(tmp_path)
    _record(ctx, toolcheck.cli_fingerprint(), probed_at=1000.0)
    assert maybe_refresh(ctx, now=2000.0) is None
    assert spawned == []


def test_an_updated_claude_is_probed_at_once(tmp_path, claude):
    ctx, spawned = _ctx(tmp_path)
    _record(ctx, toolcheck.cli_fingerprint(), probed_at=1000.0)
    claude.write_text("v2, a longer build")             # what an update does to the binary
    assert maybe_refresh(ctx, now=1010.0) == "updated"
    assert len(spawned) == 1


def test_an_update_skips_the_retry_wait(tmp_path, claude):
    ctx, spawned = _ctx(tmp_path)
    maybe_refresh(ctx, now=1000.0)                     # an attempt for the old binary
    claude.write_text("v2, a longer build")
    assert maybe_refresh(ctx, now=1010.0) == "missing"
    assert len(spawned) == 2


def test_a_day_old_catalog_is_probed_again(tmp_path, claude):
    # An MCP server added or removed changes the tool list without changing `claude`.
    ctx, spawned = _ctx(tmp_path)
    _record(ctx, toolcheck.cli_fingerprint(), probed_at=1000.0)
    assert maybe_refresh(ctx, now=1000.0 + toolcheck.CATALOG_MAX_AGE + 1) == "old"
    assert len(spawned) == 1


def test_other_hosts_and_a_missing_claude_start_nothing(tmp_path, claude, monkeypatch):
    ctx, spawned = _ctx(tmp_path, host="copilot")
    assert maybe_refresh(ctx) is None
    ctx, spawned = _ctx(tmp_path)
    monkeypatch.setattr(toolcheck, "claude_cli", lambda: str(tmp_path / "nowhere" / "claude"))
    assert maybe_refresh(ctx) is None
    assert spawned == []


def test_the_probe_records_the_fingerprint_it_ran_for(tmp_path, claude, monkeypatch):
    monkeypatch.setattr(toolcheck, "_init_tools", lambda extra, cli=None: (["Read", "Bash"], "9.9"))
    path, tools = toolcheck.refresh_catalog(str(tmp_path / "state"), cli=str(claude))
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    assert tools == ["Bash", "Read"]
    assert data["cli_fingerprint"] == toolcheck.cli_fingerprint(str(claude))
    assert toolcheck.catalog_stale(str(tmp_path / "state"), "claude",
                                   toolcheck.cli_fingerprint(str(claude))) is None


@pytest.mark.parametrize("setting, called", [("auto", True), ("off", False)])
def test_session_start_asks_unless_toolprune_is_off(env, repo, monkeypatch, setting, called):
    seen = []
    monkeypatch.setattr(toolcheck, "maybe_refresh_catalog", lambda ctx: seen.append(ctx.host.name))
    call(dict(env, TE_TOOLPRUNE=setting),
         {"hook_event_name": "SessionStart", "source": "startup", "session_id": "s1",
          "transcript_path": "", "cwd": repo})
    assert seen == (["claude"] if called else [])


def test_a_failing_refresh_does_not_cost_the_session_start(env, repo, monkeypatch):
    def boom(ctx):
        raise OSError(30, "Read-only file system")
    monkeypatch.setattr(toolcheck, "maybe_refresh_catalog", boom)
    out = call(env, {"hook_event_name": "SessionStart", "source": "startup", "session_id": "s1",
                     "transcript_path": "", "cwd": repo})
    assert isinstance(out, dict)
