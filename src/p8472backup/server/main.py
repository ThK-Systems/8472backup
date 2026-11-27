#!/usr/bin/env python3
"""Entry point for the 8472backup server daemon."""

import asyncio

from p8472backup.server import trusted_time
from p8472backup.server.trusted_time import TrustedTime, TrustedTimeError


VERSION: str = "0.1-dev"


def main() -> None:
    """Start the server daemon."""
    try:
        asyncio.run(_run_server())
    except TrustedTimeError as error:
        raise SystemExit(str(error)) from error


async def _run_server() -> None:
    clock = TrustedTime()
    try:
        await clock.synchronize()
        trusted_time.server_clock = clock
        try:
            clock_task = asyncio.create_task(clock.run(), name="trusted-time")
            await clock_task
        finally:
            del trusted_time.server_clock
    finally:
        clock.close()


if __name__ == "__main__":
    main()
