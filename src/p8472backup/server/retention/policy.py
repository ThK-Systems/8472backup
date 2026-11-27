"""Parse prune rules and select snapshots by UTC calendar periods."""

import re
from collections.abc import Mapping
from datetime import UTC, datetime
from uuid import UUID


_UNITS = "hdwMqWy"
_RULE = re.compile(rf"([0-9]+)([{_UNITS}])")


def parse_prune_rule(rule: object) -> dict[str, int]:
    """Read positive counts with at most one count per calendar unit."""
    if not isinstance(rule, str):
        raise ValueError("Prune rule must be a string")

    counts: dict[str, int] = {}
    position = 0
    while position < len(rule):
        match = _RULE.match(rule, position)
        if match is None:
            raise ValueError(f"Invalid prune rule at position {position}")
        count, unit = int(match[1]), match[2]
        if count <= 0 or unit in counts:
            raise ValueError(f"Prune rule requires one positive count per unit: {unit}")
        counts[unit] = count
        position = match.end()
    return counts


def _calendar_period(timestamp: datetime, unit: str) -> tuple[int, ...]:
    timestamp = timestamp.astimezone(UTC)
    if unit in ("w", "W"):
        year, week, _ = timestamp.isocalendar()
        return year, week if unit == "w" else (week - 1) // 13 + 1

    year, month, day = timestamp.year, timestamp.month, timestamp.day
    if unit == "h":
        return year, month, day, timestamp.hour
    if unit == "d":
        return year, month, day
    if unit == "M":
        return year, month
    if unit == "q":
        return year, (month - 1) // 3 + 1
    return (year,)


def retained_snapshots(commits: Mapping[UUID, datetime], counts: Mapping[str, int]) -> set[UUID]:
    """Choose the newest snapshot in each requested occupied calendar period."""
    if not counts:
        return set(commits)

    ordered = sorted(commits.items(), key=lambda entry: (entry[1], entry[0]), reverse=True)
    keep: set[UUID] = set()
    oldest: datetime | None = None
    for unit in _UNITS:
        remaining = counts.get(unit, 0)
        if not remaining:
            continue
        cutoff = _calendar_period(oldest, unit) if oldest is not None else None
        for snapshot_id, committed_at in ordered:
            period = _calendar_period(committed_at, unit)
            if cutoff is not None and period >= cutoff:
                continue
            keep.add(snapshot_id)
            oldest, cutoff = committed_at, period
            remaining -= 1
            if remaining == 0:
                break
    return keep
