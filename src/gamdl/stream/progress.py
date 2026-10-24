"""Native transfer progress snapshots, independent of console UI."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StreamProgress:
    """Snapshot of native transfer counters.

    downloaded_bytes includes retries; emitted_bytes counts bytes received by
    the Python iterator after optional decryption. total_bytes is known only
    when every HLS part has an explicit byte range. completed_parts counts
    fragments (including initialization sections) fully sent by Rust.
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
        """Approximate completion based on ordered, fully emitted parts."""
        return (
            100.0 * self.completed_parts / self.total_parts if self.total_parts else 0.0
        )
