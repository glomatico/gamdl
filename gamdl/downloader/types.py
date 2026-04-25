import uuid
from dataclasses import dataclass, field

from ..interface.types import AppleMusicMedia


@dataclass
class DownloadItem:
    media: AppleMusicMedia
    uuid_: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    staged_path: str = None
    final_path: str = None
    playlist_file_path: str = None
    synced_lyrics_path: str = None
    cover_path: str = None
