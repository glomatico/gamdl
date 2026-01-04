from dataclasses import dataclass
from typing import Any

from ..interface.types import (
    DecryptionKeyAv,
    Lyrics,
    MediaTags,
    PlaylistTags,
    StreamInfoAv,
)


@dataclass
class DownloadItem:
    media_metadata: dict = None
    playlist_metadata: dict = None
    random_uuid: str = None
    lyrics: Lyrics = None
    media_tags: MediaTags = None
    extra_tags: dict = None
    playlist_tags: PlaylistTags = None
    stream_info: StreamInfoAv = None
    decryption_key: DecryptionKeyAv = None
    cover_url_template: str = None
    cover_url: str = None
    staged_path: str = None
    final_path: str = None
    playlist_file_path: str = None
    synced_lyrics_path: str = None
    cover_path: str = None
    flat_filter_result: Any = None
    error: Exception = None


@dataclass
class UrlInfo:
    storefront: str = None
    type: str = None
    slug: str = None
    id: str = None
    sub_id: str = None
    library_storefront: str = None
    library_type: str = None
    library_id: str = None
