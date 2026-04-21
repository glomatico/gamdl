import sqlite3
from pathlib import Path


class Database:
    def __init__(self, path: Path):
        self.connection = sqlite3.connect(path)
        self.cursor = self.connection.cursor()
        self._create_tables()

    def _create_tables(self) -> None:
        self.cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS media (
                id TEXT PRIMARY KEY,
                path TEXT NOT NULL
            )
            """
        )
        self.connection.commit()

    def get(self, media_id: str) -> str | None:
        self.cursor.execute("SELECT path FROM media WHERE id = ?", (media_id,))
        row = self.cursor.fetchone()
        return row[0] if row else None

    def add(self, media_id: str, path: str) -> None:
        self.cursor.execute(
            "INSERT OR REPLACE INTO media (id, path) VALUES (?, ?)",
            (media_id, str(Path(path).absolute())),
        )
        self.connection.commit()

    def remove(self, media_id: str) -> None:
        self.cursor.execute("DELETE FROM media WHERE id = ?", (media_id,))
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def flat_filter(self, media_metadata: dict) -> str | None:
        media_id = media_metadata["id"]
        result = self.get(media_id)

        if not result:
            return None

        return result if Path(result).exists() else None
