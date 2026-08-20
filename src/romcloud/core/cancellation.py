"""Small provider-neutral cancellation primitive for cache transfers."""

from __future__ import annotations

import threading

from romcloud.core.exceptions import TransferCancelledError


class TransferCancellationToken:
    """Thread-safe cancellation signal shared by UI, cache, and transfer layers."""

    def __init__(self) -> None:
        self._cancelled = threading.Event()

    def cancel(self) -> None:
        self._cancelled.set()

    @property
    def is_cancelled(self) -> bool:
        return self._cancelled.is_set()

    def raise_if_cancelled(self) -> None:
        if self.is_cancelled:
            raise TransferCancelledError("Transfer cancelled by user")
