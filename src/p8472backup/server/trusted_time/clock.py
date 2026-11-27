"""Maintain the server's trusted UTC reference."""

import asyncio
import math
import socket
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from ipaddress import IPv6Address
from statistics import median
from threading import Lock
from urllib.parse import urlsplit

from p8472backup.common.log import Log
from p8472backup.server.trusted_time import _https


DEFAULT_SOURCES = (
    "https://www.google.com/",
    "https://www.microsoft.com/",
    "https://www.cloudflare.com/",
    "https://www.apple.com/",
    "https://www.amazon.com/",
    "https://www.ibm.com/",
    "https://www.wikipedia.org/",
)
DEFAULT_REFRESH_INTERVAL = 3600.0
_SYNC_TIMEOUT = 5.0
_QUORUM_SPREAD = 3.0
_MAX_CORRECTION = 10.0
_SUSPEND_TOLERANCE = 1.0
_CHECK_INTERVAL = 1.0
_DATE_PRECISION = 1.0
_DRIFT_PER_SECOND = 0.001  # Provisional allowance pending review on the server hardware.


class TrustedTimeError(RuntimeError):
    """The server cannot establish or use its trusted UTC clock."""


class TrustedTime:
    """Establish a UTC reference and refresh it in the server's event loop."""

    def __init__(self, sources: Sequence[str] = DEFAULT_SOURCES, *, refresh_interval: float = DEFAULT_REFRESH_INTERVAL) -> None:
        self._sources = tuple(sources)
        if not self._sources:
            raise ValueError("At least one trusted time source is required")
        if len({_source_host(source) for source in self._sources}) != len(self._sources):
            raise ValueError("Duplicate trusted time source host")
        if not math.isfinite(refresh_interval) or refresh_interval <= 0:
            raise ValueError("Trusted time refresh interval must be finite and positive")

        self._quorum = len(self._sources) // 2 + 1
        self._refresh_interval = refresh_interval
        self._lock = Lock()
        self._failure: str | None = None
        self._reference: tuple[float, float, float, float] | None = None
        try:
            self._boot_offset = _boot_offset()
        except OSError as error:
            raise TrustedTimeError(f"Cannot establish trusted time: {error}") from error

    def now(self) -> datetime:
        """Return the current UTC estimate or raise if the clock is invalid."""
        with self._lock:
            return self._reading()[0]

    def bounds(self) -> tuple[datetime, datetime]:
        """Return provisional UTC bounds for commit and expiry decisions."""
        with self._lock:
            _, lower, upper = self._reading()
        return lower, upper

    async def synchronize(self) -> None:
        """Measure and install a UTC reference, or invalidate the clock on failure."""
        try:
            with self._lock:
                self._require_open()
            samples = await _https.collect(self._sources, _SYNC_TIMEOUT)
            anchor = max((received for _, _, received in samples), default=0.0)
            estimate, lower, upper, agreeing = _select_reference(samples, anchor, self._quorum)

            with self._lock:
                self._require_open()
                if self._reference is not None:
                    previous_anchor, previous_estimate, _, _ = self._reference
                    self._elapsed(previous_anchor)
                    predicted = previous_estimate + (anchor - previous_anchor)
                    correction = estimate - predicted
                    if abs(correction) > _MAX_CORRECTION:
                        raise self._invalidate(f"Trusted time correction {correction:+.3f}s exceeds the {_MAX_CORRECTION:g}s limit")
                self._reference = anchor, estimate, lower, upper
                self._reading()
        except Exception as error:
            with self._lock:
                reason = str(error) if isinstance(error, TrustedTimeError) else f"Trusted time synchronization failed: {error}"
                raise self._invalidate(reason) from error
        if agreeing < len(self._sources):
            Log.warning("Trusted time synchronized with %d of %d sources agreeing", agreeing, len(self._sources))
        else:
            Log.info("Trusted time synchronized sources=%d", agreeing)

    async def run(self) -> None:
        """Refresh an initialized clock until failure or task cancellation."""
        try:
            while True:
                with self._lock:
                    anchor, _, _, _ = self._current_reference()
                    delay = self._refresh_interval - self._elapsed(anchor)
                if delay > 0:
                    await asyncio.sleep(min(delay, _CHECK_INTERVAL))
                else:
                    await self.synchronize()
        finally:
            self.close()

    def close(self) -> None:
        """Invalidate the clock when the server stops."""
        with self._lock:
            if self._failure is None:
                self._failure = "Trusted time is closed"

    def _require_open(self) -> None:
        if self._failure is not None:
            raise TrustedTimeError(self._failure)

    def _current_reference(self) -> tuple[float, float, float, float]:
        self._require_open()
        if self._reference is None:
            raise TrustedTimeError("Trusted time is not synchronized")
        return self._reference

    def _elapsed(self, anchor: float) -> float:
        """Check the reference under the clock lock and return its age."""
        try:
            elapsed = _https.monotonic() - anchor
            suspended = _boot_offset() - self._boot_offset
        except OSError as error:
            raise self._invalidate(f"Cannot read monotonic clocks: {error}") from error
        if abs(suspended) > _SUSPEND_TOLERANCE:
            raise self._invalidate("System suspend invalidated trusted time")
        # The previous and current rounds may both use their full network budget.
        if elapsed < 0 or elapsed >= self._refresh_interval + 2 * _SYNC_TIMEOUT + _CHECK_INTERVAL:
            raise self._invalidate("Trusted time reference expired")
        return elapsed

    def _reading(self) -> tuple[datetime, datetime, datetime]:
        anchor, estimate, lower, upper = self._current_reference()
        elapsed = self._elapsed(anchor)
        margin = _SUSPEND_TOLERANCE + elapsed * _DRIFT_PER_SECOND
        try:
            return (
                datetime.fromtimestamp(estimate + elapsed, UTC),
                datetime.fromtimestamp(lower + elapsed - margin, UTC),
                datetime.fromtimestamp(upper + elapsed + margin, UTC),
            )
        except (OverflowError, OSError, ValueError) as error:
            raise self._invalidate("Trusted time is outside the supported UTC range") from error

    def _invalidate(self, reason: str) -> TrustedTimeError:
        if self._failure is None:
            self._failure = reason
        return TrustedTimeError(self._failure)


def _boot_offset() -> float:
    # BOOTTIME and MONOTONIC use the same rate. Only BOOTTIME includes suspend.
    return time.clock_gettime(time.CLOCK_BOOTTIME) - time.monotonic()


def _source_host(source: str) -> str:
    url = urlsplit(source)
    host = (url.hostname or "").removesuffix(".")
    if url.scheme != "https" or not host or not source.isascii() or url.username is not None or "#" in source or "%" in host or url.port == 0:
        raise ValueError("Trusted time sources must be HTTPS URLs with a hostname and no credentials or fragment")
    if ":" in host:
        address = IPv6Address(host)
        return str(address.ipv4_mapped or address)
    try:
        return socket.inet_ntoa(socket.inet_aton(host))
    except OSError:
        return host


def _select_reference(samples: Sequence[_https.Sample], anchor: float, quorum: int) -> tuple[float, float, float, int]:
    # Project each Date from the middle of its request interval onto the same clock instant.
    estimates: list[tuple[float, float]] = []
    for date, sent, received in samples:
        half_rtt = (received - sent) / 2
        estimate = date + (anchor - sent - half_rtt)
        uncertainty = _DATE_PRECISION + half_rtt + (anchor - sent) * _DRIFT_PER_SECOND
        estimates.append((estimate, uncertainty))
    estimates.sort()
    left = best_start = best_end = 0
    ambiguous = False
    for end, (estimate, _) in enumerate(estimates, start=1):
        while estimate - estimates[left][0] > _QUORUM_SPREAD:
            left += 1
        size = end - left
        if size > best_end - best_start:
            best_start, best_end = left, end
            ambiguous = False
        elif size == best_end - best_start:
            ambiguous = True

    if best_end - best_start < quorum:
        raise TrustedTimeError(f"No trusted time quorum ({best_end - best_start} agreeing, {quorum} required)")
    if ambiguous:
        raise TrustedTimeError("Ambiguous trusted time quorum")
    group = estimates[best_start:best_end]
    # At least one source in a majority must be accurate for these bounds to hold.
    estimate = median(value for value, _ in group)
    lower = min(value - error for value, error in group)
    upper = max(value + error for value, error in group)
    return estimate, lower, upper, len(group)
