"""Internal UTC clock for the backup server."""

from p8472backup.server.trusted_time.clock import DEFAULT_REFRESH_INTERVAL, DEFAULT_SOURCES, TrustedTime, TrustedTimeError


# The daemon binds the clock before starting any operation that reads it.
server_clock: TrustedTime | None = None

__all__ = ["DEFAULT_REFRESH_INTERVAL", "DEFAULT_SOURCES", "TrustedTime", "TrustedTimeError", "server_clock"]
