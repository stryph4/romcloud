"""Focused tests for bounded SFTP transfer prefetching."""

from __future__ import annotations

from romcloud.infrastructure.providers.sftp import (
    _PrefetchingSFTPFile,
    _SFTP_PREFETCH_REQUESTS,
)


class _FakeSFTPFile:
    def __init__(self, *, fail_prefetch: bool = False) -> None:
        self.position = 0
        self.fail_prefetch = fail_prefetch
        self.prefetch_calls: list[tuple[int, int | None]] = []
        self.read_calls: list[int | None] = []

    def seek(self, offset: int, whence: int = 0) -> None:
        assert whence == 0
        self.position = offset

    def prefetch(
        self,
        file_size=None,
        max_concurrent_requests: int | None = None,
    ) -> None:
        if self.fail_prefetch:
            raise OSError("prefetch unavailable")
        self.prefetch_calls.append((self.position, max_concurrent_requests))

    def read(self, size=None):
        self.read_calls.append(size)
        return b"payload"


def test_prefix_verification_read_does_not_prefetch() -> None:
    raw = _FakeSFTPFile()
    wrapped = _PrefetchingSFTPFile(raw)  # type: ignore[arg-type]

    assert wrapped.read(1024) == b"payload"
    assert raw.prefetch_calls == []


def test_seek_arms_bounded_prefetch_from_resume_offset_once() -> None:
    raw = _FakeSFTPFile()
    wrapped = _PrefetchingSFTPFile(raw)  # type: ignore[arg-type]

    wrapped.seek(8 * 1024 * 1024)
    assert raw.prefetch_calls == []

    assert wrapped.read(1024 * 1024) == b"payload"
    assert raw.prefetch_calls == [
        (8 * 1024 * 1024, _SFTP_PREFETCH_REQUESTS)
    ]

    assert wrapped.read(1024 * 1024) == b"payload"
    assert len(raw.prefetch_calls) == 1


def test_prefetch_failure_falls_back_to_normal_reads() -> None:
    raw = _FakeSFTPFile(fail_prefetch=True)
    wrapped = _PrefetchingSFTPFile(raw)  # type: ignore[arg-type]

    wrapped.seek(0)
    assert wrapped.read(4096) == b"payload"
    assert raw.read_calls == [4096]

    # The optimization is attempted once; a server that rejects prefetch
    # must not make the transfer itself fail or repeatedly retry it.
    assert wrapped.read(4096) == b"payload"
    assert raw.read_calls == [4096, 4096]
