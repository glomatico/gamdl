"""Type declarations for the PyO3 native extension."""

from collections.abc import Awaitable

class NativeStream:
    @staticmethod
    def progressive(
        url: str,
        chunk_size: int = 262144,
        retries: int = 3,
    ) -> NativeStream: ...
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
        music_video_cbcs: bool = False,
    ) -> None: ...
    def next_chunk(self) -> Awaitable[bytes | None]: ...
    def close(self) -> None: ...
    def progress(self) -> tuple[int, int, int, int, int | None, float]: ...

def write_mp4_tags(
    path: str,
    tags: dict[str, object],
    cover_bytes: bytes | None = None,
) -> None: ...
def read_mp4_organize_tags(path: str) -> dict[str, object]: ...
def read_mp3_organize_tags(path: str) -> dict[str, object]: ...
def remux_mp4(input: bytes, repair_alac: bool = True) -> bytes: ...
def remux_mp4_file(input: str, output: str, repair_alac: bool = True) -> None: ...
def mux_av_mp4(video: bytes, audio: bytes, mp4_brand: bool = False) -> bytes: ...
