"""Collect HTTPS Date samples in a child process with a shared deadline."""

import asyncio
import re
import ssl
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import suppress
from datetime import UTC
from email.utils import format_datetime, parsedate_to_datetime
from http.client import HTTPException, HTTPResponse
from pathlib import Path
from secrets import token_hex
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPSHandler, OpenerDirector, Request


type Sample = tuple[float, float, float]  # UTC timestamp, monotonic start, monotonic receipt.
_WORKER = Path(__file__).resolve()
_MONOTONIC = getattr(time, "CLOCK_MONOTONIC_RAW", time.CLOCK_MONOTONIC)


def monotonic() -> float:
    return time.clock_gettime(_MONOTONIC)


async def collect(sources: tuple[str, ...], timeout: float) -> list[Sample]:
    started = monotonic()
    deadline = started + timeout
    command = (sys.executable, "-I", str(_WORKER), str(deadline), *sources)
    worker = await asyncio.create_subprocess_exec(*command, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    reply = asyncio.create_task(worker.communicate())
    try:
        completed, _ = await asyncio.wait((reply,), timeout=max(0.0, deadline - monotonic()))
    finally:
        # A socket timeout cannot interrupt DNS. Kill the worker while preserving pipe output.
        if worker.returncode is None:
            with suppress(ProcessLookupError):
                worker.kill()
        try:
            output, _ = await asyncio.shield(reply)
        except asyncio.CancelledError:
            await reply
            raise
    if completed and worker.returncode:
        raise OSError(f"Trusted time HTTPS worker exited with status {worker.returncode}")

    samples: list[Sample] = []
    for line in output.splitlines(keepends=True):
        if not line.endswith(b"\n"):
            break
        date, sent, received = map(float, line.split())
        if started <= sent <= received <= deadline:
            samples.append((date, sent, received))
    return samples


def _query(source: str, deadline: float, context: ssl.SSLContext) -> Sample:
    url = urlsplit(source)
    nonce = f"_8472time={token_hex(16)}"
    query = f"{url.query}&{nonce}" if url.query else nonce
    request = Request(urlunsplit((url.scheme, url.netloc, url.path, query, "")), headers={"Cache-Control": "no-cache, no-store, max-age=0"}, method="HEAD")  # noqa: S310
    opener = OpenerDirector()
    opener.add_handler(HTTPSHandler(context=context))  # No proxy or redirect handlers.

    sent = monotonic()
    if sent >= deadline:
        raise TimeoutError("Trusted time deadline expired")
    response: HTTPResponse = opener.open(request, timeout=deadline - sent)
    with response:
        received = monotonic()
        if not 200 <= response.status < 300:
            raise ValueError(f"Unexpected HTTP status {response.status}")
        dates: list[str] = response.headers.get_all("Date", [])
        ages: list[str] = response.headers.get_all("Age", [])
        if len(dates) != 1:
            raise ValueError("Expected exactly one Date header")
        if ages and (len(ages) != 1 or re.fullmatch(r"0+", ages[0].strip(" \t")) is None):
            raise ValueError("Invalid or nonzero Age header")
        header = dates[0].strip(" \t")
        date = parsedate_to_datetime(header)
        if date.tzinfo != UTC or format_datetime(date, usegmt=True) != header:
            raise ValueError("Invalid Date header")
    return date.timestamp(), sent, received


def _run_worker(deadline: float, sources: list[str]) -> None:
    context = ssl.create_default_context()
    with ThreadPoolExecutor(max_workers=len(sources)) as pool:
        queries = [pool.submit(_query, source, deadline, context) for source in sources]
        for query in as_completed(queries):
            try:
                date, sent, received = query.result()
            except (HTTPException, OSError, ValueError):
                # The parent reports unusable samples when it evaluates the round.
                continue
            print(date, sent, received, flush=True)


if __name__ == "__main__":
    _run_worker(float(sys.argv[1]), sys.argv[2:])
