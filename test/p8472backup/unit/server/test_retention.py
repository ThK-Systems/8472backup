from datetime import UTC, datetime
from uuid import UUID

import pytest

from p8472backup.server.retention.policy import parse_prune_rule, retained_snapshots


@pytest.mark.parametrize("rule", (None, "0d", "-1d", "7d2d", "1m", "1last", "7d4", "7d 4w"))
def test_prune_rule_rejects_invalid_counts_units_and_separators(rule: object) -> None:
    with pytest.raises(ValueError):
        parse_prune_rule(rule)


def test_retention_selects_the_newest_snapshot_in_each_occupied_period() -> None:
    timestamps = (
        "2026-11-27T00:50:00Z",
        "2026-11-27T01:10:00+01:00",
        "2026-11-26T23:59:00Z",
        "2026-11-26T22:00:00Z",
        "2026-11-24T12:00:00Z",
        "2026-11-24T05:00:00Z",
        "2026-11-19T16:00:00Z",
        "2026-11-16T11:00:00Z",
        "2026-11-10T08:00:00Z",
        "2026-11-09T07:00:00Z",
        "2026-10-20T13:00:00Z",
        "2026-08-20T13:00:00Z",
        "2026-08-02T13:00:00Z",
        "2026-05-10T13:00:00Z",
        "2026-03-10T13:00:00Z",
        "2024-12-31T23:59:00Z",
        "2022-01-01T00:00:00Z",
    )
    commits = {UUID(int=number): datetime.fromisoformat(timestamp) for number, timestamp in enumerate(timestamps, start=1)}
    assert retained_snapshots(commits, parse_prune_rule("1y1q1W1M2d1w2h")) == {UUID(int=number) for number in (1, 3, 5, 7, 9, 11, 12, 14, 16)}
    assert retained_snapshots(commits, parse_prune_rule("")) == set(commits)


def test_weekly_retention_uses_the_iso_year() -> None:
    commits = {
        UUID(int=1): datetime.fromisoformat("2026-01-04T23:30:00Z"),
        UUID(int=2): datetime.fromisoformat("2025-12-29T00:30:00Z"),
        UUID(int=3): datetime.fromisoformat("2025-12-28T23:30:00Z"),
    }
    assert retained_snapshots(commits, parse_prune_rule("2w")) == {UUID(int=1), UUID(int=3)}


@pytest.mark.parametrize(
    ("dates", "kept"),
    (
        pytest.param(("2026-11-26", "2026-11-24", "2026-11-23", "2026-11-20", "2026-11-09", "2026-10-30"), (1, 2, 4, 6), id="gaps-and-cutoffs"),
        pytest.param(("2026-03-03", "2026-03-02", "2026-03-01", "2026-02-28", "2026-02-27"), (1, 2, 3, 4), id="adjacent-boundaries"),
    ),
)
def test_day_week_and_month_rules_respect_calendar_boundaries(dates: tuple[str, ...], kept: tuple[int, ...]) -> None:
    commits = {UUID(int=number): datetime.fromisoformat(date).replace(tzinfo=UTC) for number, date in enumerate(dates, start=1)}
    assert retained_snapshots(commits, parse_prune_rule("2d1w1M")) == {UUID(int=number) for number in kept}
