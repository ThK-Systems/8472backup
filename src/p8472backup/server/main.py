#!/usr/bin/env python3
"""Entry point for the 8472backup server daemon."""

import asyncio

from p8472backup.server.trusted_time import TrustedTime, TrustedTimeError


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
        clock_task = asyncio.create_task(clock.run(), name="trusted-time")
        await clock_task
    finally:
        clock.close()


if __name__ == "__main__":
    main()
