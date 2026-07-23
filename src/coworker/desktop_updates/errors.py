from __future__ import annotations

from .models import RateLimitInfo


class SourceError(RuntimeError):
    """Base error for an upstream desktop-update source."""


class UnsafeSourceURLError(SourceError):
    pass


class SourceAPIError(SourceError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class RateLimitError(SourceAPIError):
    def __init__(self, message: str, rate_limit: RateLimitInfo, *, status_code: int) -> None:
        super().__init__(message, status_code=status_code)
        self.rate_limit = rate_limit


class DownloadInterruptedError(SourceError):
    pass


GitHubSourceError = SourceError
GitHubAPIError = SourceAPIError
