"""Internal UTC clock for the backup server."""

from p8472backup.server.trusted_time.clock import DEFAULT_REFRESH_INTERVAL, DEFAULT_SOURCES, TrustedTime, TrustedTimeError


# The server binds the clock before starting any operation that reads it.
server_clock: TrustedTime

__all__ = ["DEFAULT_REFRESH_INTERVAL", "DEFAULT_SOURCES", "TrustedTime", "TrustedTimeError"]
