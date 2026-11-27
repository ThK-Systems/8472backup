from pathlib import Path

import pytest

from p8472backup.server import metadata_recovery


@pytest.fixture(autouse=True)
def local_state_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(metadata_recovery, "DEFAULT_STATE_DIRECTORY", tmp_path / ".server-state")
