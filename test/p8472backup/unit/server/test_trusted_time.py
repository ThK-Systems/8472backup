# These tests exercise internal quorum and HTTPS behavior directly.
# pyright: reportPrivateUsage=false

import asyncio
import re
import signal
import ssl
import time
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from http.client import HTTPMessage, HTTPResponse
from pathlib import Path
from textwrap import dedent
from unittest.mock import AsyncMock, MagicMock, Mock
from urllib.parse import parse_qs, urlsplit
from urllib.request import HTTPSHandler, Request

import pytest

from p8472backup.server import main, trusted_time
from p8472backup.server.trusted_time import TrustedTime, TrustedTimeError, _https, clock as clock_module


_UTC = datetime(2026, 1, 1, tzinfo=UTC)
_EPOCH = _UTC.timestamp()
_DATE = format_datetime(_UTC, usegmt=True)
_SOURCES = ("https://one.example/", "https://two.example/", "https://three.example/", "https://four.example/")


def test_quorum_normalizes_to_one_instant_and_selects_the_largest_group() -> None:
    samples = [(_EPOCH - 12, 7, 9), (_EPOCH - 5, 13, 15), (_EPOCH - 2, 16, 16), (_EPOCH + 2, 19, 19), (_EPOCH + 5, 20, 20)]
    assert clock_module._select_reference(samples, 20, 3) == (_EPOCH + 1.5, _EPOCH - 2.013, _EPOCH + 4.001)


def test_quorum_rejects_tied_overlapping_groups() -> None:
    samples = [(_EPOCH + offset, 20.0, 20.0) for offset in range(5)]
    with pytest.raises(TrustedTimeError, match="Ambiguous"):
        clock_module._select_reference(samples, 20, 3)


def test_startup_requires_a_majority_of_configured_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_https, "collect", AsyncMock(return_value=[(_EPOCH, 10, 10), (_EPOCH, 10, 10)]))
    clock = TrustedTime(_SOURCES)
    with pytest.raises(TrustedTimeError, match="not synchronized"):
        clock.now()
    with pytest.raises(TrustedTimeError, match="2 agreeing, 3 required"):
        asyncio.run(clock.synchronize())
    with pytest.raises(TrustedTimeError, match="2 agreeing, 3 required"):
        clock.now()


@pytest.mark.parametrize("sources", [(_SOURCES[0], "https://ONE.example.:8443/other"), ("https://127.0.0.1/", "https://[::ffff:127.0.0.1]/")])
def test_duplicate_hosts_are_rejected(sources: tuple[str, ...]) -> None:
    with pytest.raises(ValueError, match="Duplicate"):
        TrustedTime(sources)


def test_clock_advances_from_monotonic_time_and_widens_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_https, "collect", AsyncMock(return_value=[(_EPOCH, 99, 101)]))
    monkeypatch.setattr(_https, "monotonic", lambda: 101.0)
    clock = TrustedTime(_SOURCES[:1])
    asyncio.run(clock.synchronize())
    try:
        assert clock.now() == _UTC + timedelta(seconds=1)
        monkeypatch.setattr(time, "time", lambda: -1.0)
        monkeypatch.setattr(_https, "monotonic", lambda: 113.0)
        assert clock.now() == _UTC + timedelta(seconds=13)
        assert clock.bounds() == (_UTC + timedelta(seconds=9.986), _UTC + timedelta(seconds=16.014))
    finally:
        clock.close()
    with pytest.raises(TrustedTimeError, match="closed"):
        clock.now()


@pytest.mark.parametrize(("elapsed", "expected_lower", "expected_upper"), [(999, 996.001, 1001.999), (1000, 997, 1003), (1001, 997.999, 1004.001)])
def test_clock_bounds_widen_with_elapsed_monotonic_time(monkeypatch: pytest.MonkeyPatch, elapsed: int, expected_lower: float, expected_upper: float) -> None:
    monkeypatch.setattr(_https, "monotonic", lambda: 10.0)
    monkeypatch.setattr(_https, "collect", AsyncMock(return_value=[(_EPOCH, 10, 10)]))
    clock = TrustedTime(_SOURCES[:1])
    asyncio.run(clock.synchronize())

    monkeypatch.setattr(_https, "monotonic", lambda: 10.0 + elapsed)
    estimate = clock.now()
    lower, upper = clock.bounds()
    assert estimate.tzinfo is lower.tzinfo is upper.tzinfo is UTC
    assert estimate == _UTC + timedelta(seconds=elapsed)
    assert lower == _UTC + timedelta(seconds=expected_lower)
    assert upper == _UTC + timedelta(seconds=expected_upper)


def test_expired_reference_invalidates_the_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_https, "collect", AsyncMock(return_value=[(_EPOCH, 10, 10)]))
    monkeypatch.setattr(_https, "monotonic", lambda: 10.0)
    clock = TrustedTime(_SOURCES[:1])
    asyncio.run(clock.synchronize())
    monkeypatch.setattr(_https, "monotonic", lambda: 10_000.0)
    with pytest.raises(TrustedTimeError, match="expired"):
        clock.now()
    monkeypatch.setattr(_https, "monotonic", lambda: 11.0)
    with pytest.raises(TrustedTimeError, match="expired"):
        clock.bounds()


def test_clock_refreshes_periodically(monkeypatch: pytest.MonkeyPatch) -> None:
    async def check_refresh() -> None:
        third_round = asyncio.Event()
        start = _https.monotonic()
        rounds = 0

        async def collect(*_args: object) -> list[_https.Sample]:
            nonlocal rounds
            rounds += 1
            if rounds == 3:
                third_round.set()
            now = _https.monotonic()
            return [(_EPOCH + now - start + (2 if rounds > 1 else 0), now, now)]

        monkeypatch.setattr(_https, "collect", collect)
        clock = TrustedTime(_SOURCES[:1], refresh_interval=0.02)
        await clock.synchronize()
        task = asyncio.create_task(clock.run())
        try:
            await asyncio.wait_for(third_round.wait(), timeout=2)
            assert clock.now() >= _UTC + timedelta(seconds=2)
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        with pytest.raises(TrustedTimeError, match="closed"):
            clock.now()

    asyncio.run(check_refresh())


@pytest.mark.parametrize(("offset", "failure"), [(None, "No trusted time quorum"), (30, "correction"), (-30, "correction")])
def test_failed_refresh_invalidates_and_signals_shutdown(monkeypatch: pytest.MonkeyPatch, offset: int | None, failure: str) -> None:
    first = True

    async def collect(*_args: object) -> list[_https.Sample]:
        nonlocal first
        now = _https.monotonic()
        if first:
            first = False
            return [(_EPOCH, now, now)]
        return [] if offset is None else [(_EPOCH + offset, now, now)]

    monkeypatch.setattr(_https, "collect", collect)
    clock = TrustedTime(_SOURCES[:1], refresh_interval=0.02)

    async def check_failure() -> None:
        await clock.synchronize()
        task = asyncio.create_task(clock.run())
        with pytest.raises(TrustedTimeError, match=failure):
            await asyncio.wait_for(task, timeout=2)
        with pytest.raises(TrustedTimeError, match=failure):
            clock.now()
        with pytest.raises(TrustedTimeError, match=failure):
            clock.bounds()

    asyncio.run(check_failure())


def test_suspend_invalidates_even_without_readers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_https, "monotonic", lambda: 10.0)
    monkeypatch.setattr(_https, "collect", AsyncMock(return_value=[(_EPOCH, 10, 10)]))
    monkeypatch.setattr(clock_module, "_boot_offset", lambda: 0.0)
    clock = TrustedTime(_SOURCES[:1])

    async def check_suspend() -> None:
        await clock.synchronize()
        monkeypatch.setattr(clock_module, "_boot_offset", lambda: 2.0)
        with pytest.raises(TrustedTimeError, match="suspend"):
            await asyncio.wait_for(clock.run(), timeout=2)

    asyncio.run(check_suspend())


@pytest.fixture
def https_head(monkeypatch: pytest.MonkeyPatch) -> tuple[MagicMock, Mock]:
    response = MagicMock(spec=HTTPResponse)
    response.__enter__.return_value = response
    response.status = 200
    response.headers = HTTPMessage()
    response.headers["Date"] = _DATE
    open_connection = Mock(return_value=response)
    monkeypatch.setattr(HTTPSHandler, "do_open", open_connection)
    return response, open_connection


def test_https_head_verifies_tls_and_bypasses_proxy_and_cache(monkeypatch: pytest.MonkeyPatch, https_head: tuple[MagicMock, Mock]) -> None:
    response, open_connection = https_head
    response.headers["Age"] = "0"
    monkeypatch.setenv("https_proxy", "http://proxy.example:8080/")
    deadline = _https.monotonic() + 5
    context = ssl.create_default_context()
    assert _https._query("https://one.example/?key=value", deadline, context)[0] == _EPOCH
    _https._query("https://one.example/?key=value", deadline, context)
    nonces: set[str] = set()
    for call in open_connection.call_args_list:
        request = call.args[1]
        assert isinstance(request, Request)
        assert call.kwargs["context"].verify_mode == ssl.CERT_REQUIRED
        assert call.kwargs["context"].check_hostname
        assert request.get_method() == "HEAD"
        assert request.host == "one.example"
        cache_control = request.get_header("Cache-control")
        assert cache_control is not None and "no-cache" in cache_control
        params = parse_qs(urlsplit(request.full_url).query)
        assert params["key"] == ["value"]
        (nonce,) = params["_8472time"]
        assert re.fullmatch(r"[0-9a-f]{32}", nonce)
        nonces.add(nonce)
    assert len(nonces) == 2


def test_https_redirect_is_not_followed(https_head: tuple[MagicMock, Mock]) -> None:
    response, open_connection = https_head
    response.status = 302
    response.headers["Location"] = "https://two.example/"
    with pytest.raises(ValueError, match="302"):
        _https._query(_SOURCES[0], _https.monotonic() + 5, ssl.create_default_context())
    open_connection.assert_called_once()


@pytest.mark.parametrize(("name", "values"), [("Age", ("1",)), ("Age", ("+0",)), ("Age", ("0", "0")), ("Date", ()), ("Date", ("invalid",)), ("Date", (_DATE, _DATE))])
def test_invalid_date_and_age_headers_are_rejected(https_head: tuple[MagicMock, Mock], name: str, values: tuple[str, ...]) -> None:
    response, _ = https_head
    del response.headers[name]
    for value in values:
        response.headers[name] = value
    with pytest.raises(ValueError, match=f"(?i){name}"):
        _https._query(_SOURCES[0], _https.monotonic() + 5, ssl.create_default_context())


def test_global_timeout_ends_blocked_dns_and_keeps_other_replies(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    worker = tmp_path / "worker.py"
    worker.write_text(
        dedent(f"""\
        import runpy
        import socket
        from http.client import HTTPMessage
        from threading import Event
        from unittest.mock import MagicMock
        from urllib.request import HTTPSHandler

        dns_started = Event()
        https_open = HTTPSHandler.https_open

        def blocked_dns(*args, **kwargs):
            dns_started.set()
            Event().wait(10)
            raise TimeoutError

        def open_or_reply(self, request):
            if request.host == "one.example":
                return https_open(self, request)
            if not dns_started.wait(2):
                raise RuntimeError("Requests did not run concurrently")
            response = MagicMock(status=200, headers=HTTPMessage())
            response.__enter__.return_value = response
            response.headers["Date"] = {_DATE!r}
            return response

        socket.getaddrinfo = blocked_dns
        HTTPSHandler.https_open = open_or_reply
        runpy.run_path({str(_https._WORKER)!r}, run_name="__main__")
        """),
        encoding="utf-8",
    )
    monkeypatch.setattr(_https, "_WORKER", worker)

    async def collect() -> list[_https.Sample]:
        task = asyncio.create_task(_https.collect(_SOURCES[:3], 1.0))
        await asyncio.sleep(0)
        assert not task.done()
        return await task

    started = _https.monotonic()
    samples = asyncio.run(collect())
    assert 0.9 <= _https.monotonic() - started < 3
    assert [date for date, _, _ in samples] == [_EPOCH, _EPOCH]


def test_cancelling_collection_terminates_the_worker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    worker = tmp_path / "worker.py"
    worker.write_text("import time\ntime.sleep(10)\n", encoding="utf-8")
    monkeypatch.setattr(_https, "_WORKER", worker)
    spawn = asyncio.create_subprocess_exec

    async def cancel_collection() -> None:
        started: asyncio.Future[asyncio.subprocess.Process] = asyncio.get_running_loop().create_future()

        async def spawn_worker(*command: str, stdin: int, stdout: int) -> asyncio.subprocess.Process:
            process = await spawn(*command, stdin=stdin, stdout=stdout)
            started.set_result(process)
            return process

        monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn_worker)
        task = asyncio.create_task(_https.collect(_SOURCES[:1], 5.0))
        process = await asyncio.wait_for(started, timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)
        assert process.returncode == -signal.SIGKILL

    asyncio.run(cancel_collection())


def test_daemon_exits_on_startup_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    collect = AsyncMock(return_value=[])
    monkeypatch.setattr(_https, "collect", collect)
    refresh = AsyncMock()
    monkeypatch.setattr(TrustedTime, "run", refresh)
    for _ in range(2):
        with pytest.raises(SystemExit, match="No trusted time quorum"):
            main.main()
        assert not hasattr(trusted_time, "server_clock")
    assert collect.await_count == 2
    refresh.assert_not_called()


def test_daemon_starts_refresh_task_after_synchronization(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = Mock(spec=TrustedTime)

    async def run() -> None:
        clock.synchronize.assert_awaited_once_with()
        assert trusted_time.server_clock is clock
        task = asyncio.current_task()
        assert task is not None and task.get_name() == "trusted-time"
        raise TrustedTimeError("Trusted time refresh failed")

    clock.run.side_effect = run
    monkeypatch.setattr(main, "TrustedTime", lambda: clock)
    with pytest.raises(SystemExit, match="Trusted time refresh failed"):
        main.main()
    clock.run.assert_awaited_once_with()
    clock.close.assert_called_once_with()
    assert not hasattr(trusted_time, "server_clock")
