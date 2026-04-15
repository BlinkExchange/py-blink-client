# /home/shanmu/Documents/crypto/blink/py-blink-client/py_blink_client/exceptions.py
"""
Custom exceptions for the Blink CLOB client.
"""


class BlinkError(Exception):
    """Base exception for Blink client errors."""


class BlinkApiError(BlinkError):
    """
    Raised when the Blink API returns a non-2xx response.

    Attributes:
        status_code: HTTP status code.
        response_body: Raw response body text.
        method: HTTP method of the failed request.
        path: Request path.
    """

    def __init__(
        self,
        status_code: int,
        response_body: str,
        method: str = "",
        path: str = "",
    ) -> None:
        self.status_code = status_code
        self.response_body = response_body
        self.method = method
        self.path = path
        super().__init__(
            f"Blink API error {status_code} {method} {path}: {response_body}"
        )


class BlinkAuthError(BlinkError):
    """Raised when authentication fails (missing credentials, bad signature, etc.)."""


class BlinkOrderError(BlinkError):
    """Raised when order creation or signing fails."""


class BlinkWebSocketError(BlinkError):
    """Raised on WebSocket-related errors."""


# ---------------------------------------------------------------------------
# Polymarket compatibility alias
# ---------------------------------------------------------------------------
PolyApiException = BlinkApiError
