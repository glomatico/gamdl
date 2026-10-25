"""Native transfer progress snapshots, independent of console UI."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StreamProgress:
    """Snapshot of native transfer counters.

    downloaded_bytes includes retries; emitted_bytes counts bytes received by
    the Python iterator after optional decryption. total_bytes is known when
    all HLS parts have byte ranges or progressive HTTP exposes Content-Length.
    completed_parts counts completed HLS fragments or a whole HTTP file.
    """

    downloaded_bytes: int
    emitted_bytes: int
    completed_parts: int
    total_parts: int
    total_bytes: int | None
    elapsed_seconds: float

    @property
    def speed(self) -> float:
        """Average network throughput in bytes per second."""
        return (
            self.downloaded_bytes / self.elapsed_seconds
            if self.elapsed_seconds > 0
            else 0.0
        )

    @property
    def percentage(self) -> float:
        """Completion by byte count when known, otherwise completed parts."""
        if self.total_bytes is not None and self.total_bytes > 0:
            return min(100.0, 100.0 * self.emitted_bytes / self.total_bytes)
        return (
            100.0 * self.completed_parts / self.total_parts if self.total_parts else 0.0
        )
