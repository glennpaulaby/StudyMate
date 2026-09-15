"""Unit tests for the persistent ExternalMCPService session lifecycle.

No real server is spawned here: the async internals are faked so the tests
assert the *session policy* (spawn once, reuse, restart on failure) rather
than MCP wire behaviour, which tests/e2e/test_external_mcp_stdio.py covers.
"""

from __future__ import annotations

import pytest

from study_agent.tools.mcp.external import ExternalMCPError, ExternalMCPService


class _FakeResult:
    def __init__(self, text: str) -> None:
        self.content = [type("Item", (), {"text": text})()]
        self.isError = False


def _make_service() -> ExternalMCPService:
    return ExternalMCPService(command="unused-in-tests", args=[])


def test_session_is_started_once_and_reused(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _make_service()
    starts = 0
    calls: list[tuple[str, dict]] = []

    async def fake_call_on_loop(name, arguments):
        nonlocal starts
        if not calls:
            starts += 1
        calls.append((name, arguments))
        return _FakeResult(f"result-{len(calls)}")

    monkeypatch.setattr(service, "_call_on_loop", fake_call_on_loop)

    assert service.call_tool("search", {"query": "a"}) == "result-1"
    assert service.call_tool("fetch_content", {"url": "b"}) == "result-2"
    assert starts == 1  # one spawn for two calls
    assert calls[0] == ("search", {"query": "a"})
    assert calls[1] == ("fetch_content", {"url": "b"})
    service.close()


def test_failure_restarts_session_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _make_service()
    attempts = 0
    teardowns = 0

    async def flaky_call_on_loop(name, arguments):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("transport died")
        return _FakeResult("recovered")

    async def fake_teardown():
        nonlocal teardowns
        teardowns += 1

    monkeypatch.setattr(service, "_call_on_loop", flaky_call_on_loop)
    monkeypatch.setattr(service, "_teardown_worker", fake_teardown)

    assert service.call_tool("search", {"query": "a"}) == "recovered"
    assert attempts == 2
    assert teardowns == 1  # session restarted exactly once
    service.close()


def test_repeated_failure_raises_external_mcp_error(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _make_service()

    async def dead_call_on_loop(name, arguments):
        raise RuntimeError("server missing")

    async def fake_teardown():
        return None

    monkeypatch.setattr(service, "_call_on_loop", dead_call_on_loop)
    monkeypatch.setattr(service, "_teardown_worker", fake_teardown)

    with pytest.raises(ExternalMCPError, match="server missing"):
        service.call_tool("search", {"query": "a"})
    service.close()
