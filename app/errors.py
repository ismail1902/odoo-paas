"""Shared exception types."""


class PlatformError(Exception):
    """Base class for expected platform errors (shown to users)."""


class BuildError(PlatformError):
    """A build step failed; the message is written to the build log."""


class DeployError(PlatformError):
    """A deploy/queueing request was rejected."""
