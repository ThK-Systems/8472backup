#!/usr/bin/env python3
"""Entry point for the 8472backup server service."""

import asyncio

from p8472backup.common.log import Log
from p8472backup.server import trusted_time
from p8472backup.server.scheduler.scheduler import Scheduler
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
    clock = TrustedTime()
    try:
        await clock.synchronize()
        trusted_time.server_clock = clock
        try:
            Log.info("Server started version=%s", VERSION)
            clock_task = asyncio.create_task(clock.run(), name="trusted-time")
            scheduler_task = asyncio.create_task(Scheduler().run(), name="scheduler")
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
