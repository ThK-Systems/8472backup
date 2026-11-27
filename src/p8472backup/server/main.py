#!/usr/bin/env python3
"""Entry point for the 8472backup server service."""

import asyncio
import os
from pathlib import Path

from p8472backup.common.log import Log
from p8472backup.server import trusted_time
from p8472backup.server.scheduler.scheduler import DEFAULT_COMPACT_THRESHOLD_PERCENT, Scheduler
from p8472backup.server.snapshot_service import configured_protection_duration
from p8472backup.server.trusted_time import TrustedTime, TrustedTimeError


VERSION: str = "0.1-dev"


def main() -> None:
    """Start the server service."""
    try:
        Log.configure()
    except ValueError as error:
        Log.error("Server could not configure logging: %s", error)
        raise SystemExit(1) from error

    try:
        asyncio.run(_run_server())
    except TrustedTimeError as error:
        Log.error("Server stopped: %s", error)
        raise SystemExit(1) from error
    except KeyboardInterrupt:
        Log.info("Server stopped by operator")
        raise SystemExit(130) from None
    except Exception as error:
        Log.error("Server stopped unexpectedly reason=%s", type(error).__name__)
        raise SystemExit(1) from error
    else:
        Log.info("Server stopped")


async def _run_server() -> None:
    storage_root = os.getenv("P8472BACKUP_STORAGE_ROOT")
    threshold = os.getenv("P8472BACKUP_COMPACT_THRESHOLD_PERCENT")
    try:
        compact_threshold_percent = DEFAULT_COMPACT_THRESHOLD_PERCENT if threshold is None else int(threshold)
    except ValueError as error:
        raise ValueError("P8472BACKUP_COMPACT_THRESHOLD_PERCENT must be a whole percentage") from error
    scheduler = Scheduler(
        storage_root=Path(storage_root) if storage_root else None,
        compact_threshold_percent=compact_threshold_percent,
        protection_duration=configured_protection_duration(None),
    )
    clock = TrustedTime()
    try:
        await clock.synchronize()
        trusted_time.server_clock = clock
        try:
            Log.info("Server started version=%s", VERSION)
            clock_task = asyncio.create_task(clock.run(), name="trusted-time")
            scheduler_task = asyncio.create_task(scheduler.run(), name="scheduler")
            tasks = (clock_task, scheduler_task)
            try:
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in tasks:
                    if task.done():
                        task.result()
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            del trusted_time.server_clock
    finally:
        clock.close()


if __name__ == "__main__":
    main()
