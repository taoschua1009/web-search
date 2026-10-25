"""Stable, display-safe errors shared by every research component."""

from __future__ import annotations


class ResearchError(Exception):
    """A stable, safe error suitable for display without provider payloads."""

    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def error_view(error):
    """The public (code, message) form of an error."""
    return {"code": error.code, "message": error.message}
