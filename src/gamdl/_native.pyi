"""Type declarations for the PyO3 native extension."""

def write_mp4_tags(
    path: str,
    tags: dict[str, object],
    cover_bytes: bytes | None = None,
) -> None: ...
