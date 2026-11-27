import logging
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest

from p8472backup.archive.objects import OBJECT_ID_SIZE, TYPE_SNAPSHOT, Object
from p8472backup.common.log import Log
from p8472backup.server import main, trusted_time
from p8472backup.server.server_facade import ServerFacade
from p8472backup.storage.file.lock import RepositoryLock


_CLIENT_ID = UUID(int=1)
_SET_ID = UUID(int=2)
_KEY_ID = UUID(int=3)
_ROOT = Object(TYPE_SNAPSHOT, b"r" * OBJECT_ID_SIZE, 1)
_NOW = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def facade(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> ServerFacade:
    service = ServerFacade(tmp_path, server_fqdn="backup.example.net")
    service.init_repository(_CLIENT_ID, {_SET_ID: _KEY_ID})
    caplog.set_level(logging.INFO, logger="p8472backup")
    caplog.clear()
    return service


@pytest.mark.parametrize("mode", (None, "dev", "intg", "prod"))
def test_logging_routes_each_message_once_for_the_configured_mode(mode: str | None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture) -> None:
    if mode is None:
        monkeypatch.delenv("P8472BACKUP_ENV", raising=False)
    else:
        monkeypatch.setenv("P8472BACKUP_ENV", mode)

    logger = logging.getLogger("p8472backup")
    monkeypatch.setattr(logger, "handlers", [])
    monkeypatch.setattr(logger, "propagate", True)
    with caplog.at_level(logging.WARNING, logger=logger.name):
        try:
            Log.configure()
            Log.configure()
            Log.info("Server %s", "started")
            Log.warning("Operation deferred")
            Log.error("Server stopped")
            print("protocol output")
        finally:
            for handler in logger.handlers:
                handler.close()

    result = capsys.readouterr()
    assert caplog.records == []
    if mode == "dev":
        assert result.out.splitlines() == ["INFO Server started", "protocol output"]
        assert result.err.splitlines() == ["WARNING Operation deferred", "ERROR Server stopped"]
    else:
        assert result.out == "protocol output\n"
        assert result.err.splitlines() == ["<6>Server started", "<4>Operation deferred", "<3>Server stopped"]


def test_invalid_log_mode_reports_one_fatal_error_before_startup(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.setenv("P8472BACKUP_ENV", "invalid")
    server = AsyncMock()
    monkeypatch.setattr(main, "_run_server", server)

    with pytest.raises(SystemExit) as failure:
        main.main()

    assert failure.value.code == 1
    assert caplog.record_tuples == [("p8472backup", logging.ERROR, "Server could not configure logging: P8472BACKUP_ENV must be dev, intg, or prod")]
    server.assert_not_called()


@pytest.mark.parametrize(
    ("error", "exit_code", "level", "message"),
    (
        pytest.param(None, None, logging.INFO, "Server stopped", id="normal"),
        pytest.param(KeyboardInterrupt(), 130, logging.INFO, "Server stopped by operator", id="interrupted"),
        pytest.param(RuntimeError("private error details"), 1, logging.ERROR, "Server stopped unexpectedly reason=RuntimeError", id="fatal"),
    ),
)
def test_server_reports_shutdown_once(error: BaseException | None, exit_code: int | None, level: int, message: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    server = AsyncMock(side_effect=error)
    monkeypatch.setattr(main, "_run_server", server)
    monkeypatch.setattr(Log, "configure", lambda: None)

    with caplog.at_level(logging.INFO, logger="p8472backup"):
        if exit_code is None:
            main.main()
        else:
            with pytest.raises(SystemExit) as failure:
                main.main()
            assert failure.value.code == exit_code

    server.assert_awaited_once_with()
    assert caplog.record_tuples == [("p8472backup", level, message)]


def test_backup_logging_records_admission_commit_and_retention(facade: ServerFacade, caplog: pytest.LogCaptureFixture) -> None:
    with patch.object(trusted_time, "server_clock", create=True) as clock:
        clock.now.return_value = _NOW
        facade.backup(_CLIENT_ID, _SET_ID, iter(((_ROOT, BytesIO(b"r")),)), prune="1d")
        caplog.clear()
        clock.now.return_value = _NOW + timedelta(days=8)
        snapshot = facade.backup(_CLIENT_ID, _SET_ID, iter(((_ROOT, BytesIO(b"r")),)), prune="1d", comment="private backup comment")

    scope = f"client_id={_CLIENT_ID} backup_set_id={_SET_ID}"
    assert caplog.record_tuples == [
        ("p8472backup", logging.INFO, f"Backup started {scope} snapshot_id={snapshot.snapshot_id}"),
        ("p8472backup", logging.INFO, f"Backup committed {scope} snapshot_id={snapshot.snapshot_id}"),
        ("p8472backup", logging.INFO, f"Snapshot removal completed {scope} snapshots_removed=1"),
    ]


def test_failed_backup_emits_one_warning_and_logs_automatic_cleanup(facade: ServerFacade, caplog: pytest.LogCaptureFixture) -> None:
    body = BytesIO()
    with pytest.raises(ValueError, match="Truncated object body"):
        facade.backup(_CLIENT_ID, _SET_ID, iter(((_ROOT, body),)), prune="", auto_cleanup=True)

    scope = f"client_id={_CLIENT_ID} backup_set_id={_SET_ID}"
    records = caplog.record_tuples
    assert len(records) == 3
    assert records[0][0:2] == ("p8472backup", logging.INFO)
    assert records[0][2].startswith(f"Backup started {scope} snapshot_id=")
    snapshot_id = UUID(records[0][2].rsplit("snapshot_id=", 1)[1])
    assert records[1:] == [
        ("p8472backup", logging.INFO, f"Backup cleanup completed {scope} runs_removed=1"),
        ("p8472backup", logging.WARNING, f"Operation backup failed {scope} snapshot_id={snapshot_id} reason=ValueError"),
    ]


def test_lock_contention_warns_once_without_logging_storage_path(facade: ServerFacade, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    with RepositoryLock(tmp_path / str(_CLIENT_ID)), pytest.raises(BlockingIOError):
        facade.cleanup(_CLIENT_ID, _SET_ID)

    assert caplog.record_tuples == [
        ("p8472backup", logging.WARNING, f"Operation cleanup failed client_id={_CLIENT_ID} backup_set_id={_SET_ID} reason=BlockingIOError"),
    ]


def test_restore_logs_opening_and_completion_once(facade: ServerFacade, caplog: pytest.LogCaptureFixture) -> None:
    with patch.object(trusted_time, "server_clock", create=True) as clock:
        clock.now.return_value = _NOW
        snapshot = facade.backup(_CLIENT_ID, _SET_ID, iter(((_ROOT, BytesIO(b"r")),)), prune="")
    caplog.clear()

    with facade.restore(_CLIENT_ID, _SET_ID, snapshot.snapshot_id) as reader:
        for _ in range(2):
            reader.read_object(reader.root_id, BytesIO())

    scope = f"client_id={_CLIENT_ID} backup_set_id={_SET_ID} snapshot_id={snapshot.snapshot_id}"
    assert caplog.record_tuples == [
        ("p8472backup", logging.INFO, f"Snapshot opened for restore {scope}"),
        ("p8472backup", logging.INFO, f"Restore completed {scope}"),
    ]


def test_restore_read_failure_logs_one_warning_with_snapshot_id(facade: ServerFacade, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    with patch.object(trusted_time, "server_clock", create=True) as clock:
        clock.now.return_value = _NOW
        snapshot = facade.backup(_CLIENT_ID, _SET_ID, iter(((_ROOT, BytesIO(b"r")),)), prune="")
    (tmp_path / str(_CLIENT_ID) / "data" / "packs" / str(_KEY_ID) / "1.pack").unlink()
    caplog.clear()

    with pytest.raises(FileNotFoundError), facade.restore(_CLIENT_ID, _SET_ID, snapshot.snapshot_id) as reader:
        reader.read_object(reader.root_id, BytesIO())

    scope = f"client_id={_CLIENT_ID} backup_set_id={_SET_ID} snapshot_id={snapshot.snapshot_id}"
    assert caplog.record_tuples == [
        ("p8472backup", logging.INFO, f"Snapshot opened for restore {scope}"),
        ("p8472backup", logging.WARNING, f"Operation restore failed {scope} reason=FileNotFoundError"),
    ]
