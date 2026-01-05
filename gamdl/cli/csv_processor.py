"""CSV processing utilities for batch song downloads."""

import asyncio
import csv
import logging
import re

import click

from ..downloader import AppleMusicDownloader, GamdlError
from .constants import CSV_BATCH_DELAY_SECONDS, CSV_BATCH_SIZE
from .song_search import ItunesSearch

logger = logging.getLogger(__name__)


class CSVProcessor:
    """Processes CSV files for batch song downloads.
    
    Handles reading CSV files, searching for songs via iTunes API,
    matching tracks, and downloading them in batches.
    """

    def __init__(
        self,
        itunes_search: ItunesSearch,
        downloader: AppleMusicDownloader,
        limit: int,
        no_exceptions: bool,
    ):
        """Initialize CSV processor.
        
        Args:
            itunes_search: ItunesSearch instance for searching
            downloader: Downloader instance for processing URLs
            limit: Maximum number of search results per query
            no_exceptions: Whether to suppress exception details in logs
        """
        self.itunes_search = itunes_search
        self.downloader = downloader
        self.limit = limit
        self.no_exceptions = no_exceptions
        self.error_count = 0
        self.total_found = 0

    @staticmethod
    def read_csv_songs(csv_path: str) -> list[tuple[str, str]]:
        """Read title and artist pairs from CSV file.
        
        Args:
            csv_path: Path to CSV file with 'title' and 'artist' columns
            
        Returns:
            List of (title, artist) tuples
        """
        songs = []
        with open(csv_path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            if reader.fieldnames:
                reader.fieldnames = [name.strip() for name in reader.fieldnames]
            for row in reader:
                title = row.get("title", "").strip()
                artist = row.get("artist", "").strip()
                if title and artist:
                    songs.append((title, artist))
        return songs


    @staticmethod
    def _matches_title(csv_title: str, api_title: str) -> bool:
        """Check if API title matches CSV title (flexible matching).
        
        Args:
            csv_title: Title from CSV
            api_title: Title from API
            
        Returns:
            True if titles match
        """
        csv_lower = csv_title.lower()
        api_lower = api_title.lower()
        return (
            api_lower == csv_lower
            or api_lower.startswith(csv_lower + " (")
            or api_lower.startswith(csv_lower + " [")
            or csv_lower.startswith(api_lower + " (")
            or csv_lower.startswith(api_lower + " [")
        )

    @staticmethod
    def _matches_artist(csv_artist: str, api_artist: str) -> bool:
        """Check if API artist matches CSV artist (flexible matching with feat/collab support).
        
        Args:
            csv_artist: Artist from CSV
            api_artist: Artist from API
            
        Returns:
            True if artists match
        """
        csv_lower = csv_artist.lower()
        api_lower = api_artist.lower()
        
        if api_lower == csv_lower:
            return True
        
        split_pattern = r'[,&]|\s+(?:featuring|feat\.?|ft\.?)\s+'
        csv_parts = [
            p.strip()
            for p in re.split(split_pattern, csv_lower, flags=re.IGNORECASE)
            if p.strip()
        ]
        api_parts = [
            p.strip()
            for p in re.split(split_pattern, api_lower, flags=re.IGNORECASE)
            if p.strip()
        ]
        
        if api_lower in csv_parts or csv_lower in api_parts:
            return True
        
        if len(csv_parts) > 1:
            return any(csv_part in api_parts for csv_part in csv_parts)
        
        return False

    def _find_matching_track(
        self,
        results: dict,
        csv_title: str,
        csv_artist: str,
    ) -> dict | None:
        """Find matching track in search results.
        
        Args:
            results: Search results from iTunes API
            csv_title: Title from CSV
            csv_artist: Artist from CSV
            
        Returns:
            Matching track item or None
        """
        for item in results.get("results", []):
            item_title = item.get("trackName", "").strip()
            item_artist = item.get("artistName", "").strip()
            
            if self._matches_title(csv_title, item_title) and self._matches_artist(
                csv_artist, item_artist
            ):
                return item
        
        return None

    async def _process_url_for_download(
        self,
        url: str,
        url_progress: str,
    ) -> None:
        """Process a single URL for downloading.
        
        Args:
            url: URL to process
            url_progress: Progress string for logging
        """
        try:
            url_info = self.downloader.get_url_info(url)
            if not url_info:
                logger.warning(url_progress + f' Could not parse "{url}", skipping.')
                return
            
            download_queue = await self.downloader.get_download_queue(url_info)
            if not download_queue:
                logger.warning(
                    url_progress + f' No downloadable media found for "{url}", skipping.'
                )
                return
        except KeyboardInterrupt:
            exit(1)
        except Exception as e:
            self.error_count += 1
            logger.error(
                url_progress + f' Error processing "{url}"',
                exc_info=not self.no_exceptions,
            )
            return
        
        for item in download_queue:
            try:
                await self.downloader.download(item)
            except GamdlError:
                pass
            except KeyboardInterrupt:
                exit(1)
            except Exception:
                self.error_count += 1
                logger.error(
                    url_progress + " Error downloading",
                    exc_info=not self.no_exceptions,
                )

    async def process_csv(self, csv_path: str) -> tuple[int, int]:
        """Process CSV file and download songs in batches.
        
        Args:
            csv_path: Path to CSV file with 'title' and 'artist' columns
            
        Returns:
            Tuple of (total_found, error_count)
        """
        logger.info(f'Reading songs from "{csv_path}"...')
        csv_songs = self.read_csv_songs(csv_path)
        logger.info(
            f"Found {len(csv_songs)} songs to process (batch size: {CSV_BATCH_SIZE})"
        )
        
        if not csv_songs:
            return (0, 0)
        
        # Reset counters for this CSV processing session
        self.error_count = 0
        self.total_found = 0
        
        total_rows = len(csv_songs)
        total_batches = (total_rows + CSV_BATCH_SIZE - 1) // CSV_BATCH_SIZE
        
        for batch_start in range(0, total_rows, CSV_BATCH_SIZE):
            batch_end = min(batch_start + CSV_BATCH_SIZE, total_rows)
            batch_num = (batch_start // CSV_BATCH_SIZE) + 1
            
            logger.info(
                f"[CSV Batch {batch_num}/{total_batches}] "
                f"Searching songs {batch_start + 1}-{batch_end}..."
            )
            
            batch_urls = []
            for idx in range(batch_start, batch_end):
                title, artist = csv_songs[idx]
                query = f"{title} {artist}"
                logger.debug(f'Searching for "{query}"...')
                
                results = await self.itunes_search.search(
                    query,
                    limit=self.limit,
                    entity="song",
                    with_retry=True,
                    title=title,
                    artist=artist,
                )
                if not results:
                    continue
                
                match = self._find_matching_track(results, title, artist)
                if not match:
                    logger.warning(
                        f"No exact match found for {title} - {artist}"
                    )
                    continue
                
                track_url = match.get("songUrl", "")
                if track_url:
                    logger.info(f"Found match: {title} - {artist}")
                    batch_urls.append(track_url)
                    self.total_found += 1
                else:
                    logger.warning(
                        f"Match found but no URL for {title} - {artist}"
                    )
            
            # Download this batch immediately
            if batch_urls:
                logger.info(
                    f"[CSV Batch {batch_num}/{total_batches}] "
                    f"Downloading {len(batch_urls)} songs..."
                )
                for url_index, url in enumerate(batch_urls, 1):
                    url_progress = click.style(
                        f"[CSV {batch_num}/{total_batches} - "
                        f"{url_index}/{len(batch_urls)}]",
                        dim=True,
                    )
                    logger.info(url_progress + f' Processing "{url}"')
                    await self._process_url_for_download(url, url_progress)
            
            # Delay between batches (except after last batch)
            if batch_end < total_rows:
                logger.debug(
                    f"Batch complete. Waiting {CSV_BATCH_DELAY_SECONDS}s "
                    "before next batch..."
                )
                await asyncio.sleep(CSV_BATCH_DELAY_SECONDS)
        
        logger.info(
            f"CSV processing complete. Found and downloaded "
            f"{self.total_found} of {total_rows} songs."
        )
        
        return (self.total_found, self.error_count)


# Backward compatibility: export function for existing code
def read_csv_songs(csv_path: str) -> list[tuple[str, str]]:
    """Read CSV songs (backward compatibility wrapper)."""
    return CSVProcessor.read_csv_songs(csv_path)
