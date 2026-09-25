"""Exceptions for Kaadas Smart."""


class KaadasError(Exception):
    """Base exception for Kaadas cloud errors."""


class KaadasAuthError(KaadasError):
    """Raised when the Kaadas cloud rejects the credentials or session token."""


class KaadasConnectionError(KaadasError):
    """Raised when the Kaadas cloud cannot be reached."""
