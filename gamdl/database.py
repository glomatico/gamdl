import sqlite3
from pathlib import Path


class Database:
    INITIAL_QUERY = """
        CREATE TABLE IF NOT EXISTS media (
            media_id TEXT PRIMARY KEY,
            media_path TEXT NOT NULL
        )
    """
    ADD_MEDIA_QUERY = """
        INSERT OR REPLACE INTO media (media_id, media_path) VALUES (?, ?)
    """
    GET_MEDIA_QUERY = """
        SELECT media_path FROM media WHERE media_id = ?
    """

    def __init__(self, file_path: Path):
        self.file_path = file_path
        self._initialize_db()

    def _initialize_db(self):
        self.file_path.parent.mkdir(parents=True, exist_ok=True)

        with sqlite3.connect(self.file_path) as conn:
            conn.execute(self.INITIAL_QUERY)
            conn.commit()

    def add_media(self, media_id: str, media_path: Path):
        with sqlite3.connect(self.file_path) as conn:
            conn.execute(
                self.ADD_MEDIA_QUERY,
                (
                    media_id,
                    str(media_path.absolute()),
                ),
            )
            conn.commit()

    def get_media(self, media_id: str) -> Path | None:
        with sqlite3.connect(self.file_path) as conn:
            cursor = conn.execute(
                self.GET_MEDIA_QUERY,
                (media_id,),
            )
            result = cursor.fetchone()
            if result:
                return Path(result[0])
            return None
