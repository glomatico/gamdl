"""iTunes search functionality with URL construction and retry logic."""

import asyncio
import logging

from ..api import ItunesApi
from .constants import CSV_MAX_RETRIES, CSV_RATE_LIMIT_RETRY_SECONDS

logger = logging.getLogger(__name__)


class ItunesSearch:
    """Handles iTunes API searches with URL construction and retry logic.
    
    Wraps ItunesApi to provide:
    - Automatic URL construction for song search results
    - Rate limit retry logic
    - Enriched results with URLs (for songs only)
    """

    def __init__(self, itunes_api: ItunesApi):
        """Initialize iTunes search.
        
        Args:
            itunes_api: iTunes API instance
        """
        self.itunes_api = itunes_api

    def _construct_song_url(self, track_item: dict) -> str:
        """Construct Apple Music song URL from search result item.
        
        Args:
            track_item: Search result item with trackId, kind, collectionViewUrl
            
        Returns:
            Apple Music song URL
        """
        track_id = track_item.get("trackId")
        if not track_id:
            return ""
        
        collection_view_url = track_item.get("collectionViewUrl")
        if track_item.get("kind") == "song" and collection_view_url:
            try:
                base_url = collection_view_url.split("?")[0]
                base_url = base_url.replace("/album/", "/song/")
                parts = base_url.split("/")
                if parts and parts[-1].isdigit():
                    parts[-1] = str(track_id)
                    return "/".join(parts)
            except Exception:
                pass
        
        return f"https://music.apple.com/{self.itunes_api.storefront}/song/{track_id}"

    def _enrich_results_with_urls(self, results: dict, entity: str) -> dict:
        """Add URLs to search results (only for song entities).
        
        Args:
            results: Search results from iTunes API
            entity: Entity type that was searched (e.g., "song", "album")
            
        Returns:
            Results with 'songUrl' added to each item (only if entity is "song")
        """
        if entity == "song" and results.get("results"):
            for item in results["results"]:
                if item.get("kind") == "song":
                    song_url = self._construct_song_url(item)
                    item["songUrl"] = song_url
        return results

    async def search(
        self,
        query: str,
        limit: int = 10,
        entity: str = "song",
        with_retry: bool = False,
        title: str = None,
        artist: str = None,
    ) -> dict:
        """Search iTunes API with optional retry logic and URL enrichment.
        
        Args:
            query: Search query string
            limit: Maximum number of results
            entity: Entity type to search (default: "song")
            with_retry: Whether to use retry logic for rate limits
            title: Title (for logging when using retry)
            artist: Artist (for logging when using retry)
            
        Returns:
            Search results dict with URLs added to each item (only if entity is "song")
        """
        if with_retry:
            results = await self._search_with_retry(query, limit, entity, title, artist)
        else:
            results = await self.itunes_api.search(query, entity=entity, limit=limit)
        
        if results:
            return self._enrich_results_with_urls(results, entity)
        return results

    async def _search_with_retry(
        self,
        query: str,
        limit: int,
        entity: str,
        title: str = None,
        artist: str = None,
    ) -> dict | None:
        """Search iTunes API with rate limit retry logic.
        
        Args:
            query: Search query string
            limit: Maximum number of results
            entity: Entity type to search
            title: Title (for logging)
            artist: Artist (for logging)
            
        Returns:
            Search results dict or None if all retries failed
        """
        for retry in range(CSV_MAX_RETRIES):
            try:
                return await self.itunes_api.search(query, entity=entity, limit=limit)
            except Exception as e:
                error_str = str(e)
                if "429" in error_str or "rate limit" in error_str.lower():
                    if retry < CSV_MAX_RETRIES - 1:
                        logger.warning(
                            f"Rate limited. Waiting {CSV_RATE_LIMIT_RETRY_SECONDS}s before retry "
                            f"({retry + 1}/{CSV_MAX_RETRIES})..."
                        )
                        await asyncio.sleep(CSV_RATE_LIMIT_RETRY_SECONDS)
                    else:
                        log_msg = f"Rate limit exceeded after {CSV_MAX_RETRIES} retries"
                        if title and artist:
                            log_msg += f" for {title} - {artist}"
                        logger.error(log_msg)
                else:
                    log_msg = f"Error searching for {query}"
                    if title and artist:
                        log_msg = f"Error searching for {title} - {artist}: {e}"
                    logger.error(log_msg)
                    break
        return None

