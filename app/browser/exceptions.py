from __future__ import annotations

from app.api.exceptions import GMGNAuthError, GMGNError


class PlaywrightNotAvailable(GMGNError):
    pass


class BrowserSessionError(GMGNError):
    pass


class BrowserLoginTimeout(GMGNAuthError):
    pass


class BrowserNetworkError(GMGNError):
    pass
