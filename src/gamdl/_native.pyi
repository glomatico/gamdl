"""Type declarations for the PyO3 native extension."""

from collections.abc import Awaitable

class NativeStream:
    def __init__(
        self,
        parts: list[tuple[str, int | None, int | None]],
        concurrency: int = 8,
        chunk_size: int = 262144,
        retries: int = 3,
        decryption_key: bytes | None = None,
        key_id: bytes | None = None,
        enhanced_hls_song: bool = False,
        wrapper: tuple[str, int, str, str] | None = None,
        asset_song_cbcs: bool = False,
    ) -> None: ...
    def next_chunk(self) -> Awaitable[bytes | None]: ...
    def close(self) -> None: ...
    def progress(self) -> tuple[int, int, int, int, int | None, float]: ...

def write_mp4_tags(
    path: str,
    tags: dict[str, object],
    cover_bytes: bytes | None = None,
) -> None: ...
def remux_mp4(input: bytes) -> bytes: ...
