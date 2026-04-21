from InquirerPy import inquirer
from InquirerPy.base.control import Choice
import m3u8
from ..interface import ArtistMediaType


class InteractivePrompts:
    def __init__(
        self,
        artist_auto_select: ArtistMediaType | None = None,
    ):
        self.artist_auto_select = artist_auto_select

    @staticmethod
    def millis_to_min_sec(millis) -> str:
        minutes, seconds = divmod(millis // 1000, 60)
        return f"{minutes:02}:{seconds:02}"

    @staticmethod
    async def ask_song_codec(
        playlists: list[dict],
    ) -> dict:
        choices = [
            Choice(
                name=playlist["stream_info"]["audio"],
                value=playlist,
            )
            for playlist in playlists
        ]

        return await inquirer.select(
            message="Select which codec to download:",
            choices=choices,
        ).execute_async()

    @staticmethod
    async def ask_music_video_video_codec_function(
        playlists: list[m3u8.Playlist],
    ) -> dict:
        choices = [
            Choice(
                name=" | ".join(
                    [
                        playlist.stream_info.codecs[:4],
                        "x".join(str(v) for v in playlist.stream_info.resolution),
                        str(playlist.stream_info.bandwidth),
                    ]
                ),
                value=playlist,
            )
            for playlist in playlists
        ]

        return await inquirer.select(
            message="Select which video codec to download: (Codec | Resolution | Bitrate)",
            choices=choices,
        ).execute_async()

    @staticmethod
    async def ask_music_video_audio_codec_function(
        playlists: list[dict],
    ) -> dict:
        choices = [
            Choice(
                name=playlist["group_id"],
                value=playlist,
            )
            for playlist in playlists
        ]

        selected = await inquirer.select(
            message="Select which audio codec to download:",
            choices=choices,
        ).execute_async()

        return selected

    @staticmethod
    async def ask_uploaded_video_quality_function(
        available_qualities: dict[str, str],
    ) -> str:
        qualities = list(available_qualities.keys())
        choices = [
            Choice(
                name=quality,
                value=quality,
            )
            for quality in qualities
        ]
        selected = await inquirer.select(
            message="Select which quality to download:",
            choices=choices,
        ).execute_async()

        return available_qualities[selected]

    async def ask_artist_media_type(
        self,
        media_types: list[ArtistMediaType],
        artist_metadata: dict,
    ) -> ArtistMediaType:
        if self.artist_auto_select:
            return self.artist_auto_select

        available_choices = []
        for media_types in media_types:
            available_choices.append(
                Choice(
                    name=str(media_types),
                    value=(media_types,),
                ),
            )

        (media_type,) = await inquirer.select(
            message=f'Select which type to download for artist "{artist_metadata["attributes"]["name"]}":',
            choices=available_choices,
            validate=lambda result: artist_metadata.get(result[0].path_key[0], {})
            .get(result[0].path_key[1], {})
            .get("data"),
        ).execute_async()

        return media_type

    async def ask_artist_select_items(
        self,
        media_type: ArtistMediaType,
        items: list[dict],
    ) -> list[dict]:
        if media_type in {
            ArtistMediaType.MAIN_ALBUMS,
            ArtistMediaType.COMPILATION_ALBUMS,
            ArtistMediaType.LIVE_ALBUMS,
            ArtistMediaType.SINGLES_EPS,
            ArtistMediaType.ALL_ALBUMS,
        }:
            return await self._ask_artist_select_albums(items)
        elif media_type == ArtistMediaType.TOP_SONGS:
            return await self._ask_artist_select_songs(
                items,
            )
        elif media_type == ArtistMediaType.MUSIC_VIDEOS:
            return await self._ask_artist_select_music_videos(items)

    async def _ask_artist_select_albums(
        self,
        albums: list[dict],
    ) -> list[dict]:
        if self.artist_auto_select:
            return albums

        choices = [
            Choice(
                name=" | ".join(
                    [
                        f'{album["attributes"]["trackCount"]:03d}',
                        f'{album["attributes"]["releaseDate"]:<10}',
                        f'{album["attributes"].get("contentRating", "None").title():<8}',
                        f'{album["attributes"]["name"]}',
                    ]
                ),
                value=album,
            )
            for album in albums
            if album.get("attributes")
        ]
        selected = await inquirer.select(
            message="Select which albums to download: (Track Count | Release Date | Rating | Title)",
            choices=choices,
            multiselect=True,
        ).execute_async()

        return selected

    async def _ask_artist_select_songs(
        self,
        songs: list[dict],
    ) -> list[dict]:
        if self.artist_auto_select:
            return songs

        choices = [
            Choice(
                name=" | ".join(
                    [
                        self.millis_to_min_sec(song["attributes"]["durationInMillis"]),
                        f'{song["attributes"].get("contentRating", "None").title():<8}',
                        song["attributes"]["name"],
                    ],
                ),
                value=song,
            )
            for song in songs
            if song.get("attributes")
        ]
        selected = await inquirer.select(
            message="Select which songs to download: (Duration | Rating | Title)",
            choices=choices,
            multiselect=True,
        ).execute_async()

        return selected

    async def _ask_artist_select_music_videos(
        self,
        music_videos: list[dict],
    ) -> list[dict]:
        if self.artist_auto_select:
            return music_videos

        choices = [
            Choice(
                name=" | ".join(
                    [
                        self.millis_to_min_sec(
                            music_video["attributes"]["durationInMillis"]
                        ),
                        f'{music_video["attributes"].get("contentRating", "None").title():<8}',
                        music_video["attributes"]["name"],
                    ],
                ),
                value=music_video,
            )
            for music_video in music_videos
            if music_video.get("attributes")
        ]
        selected = await inquirer.select(
            message="Select which music videos to download: (Duration | Rating | Title)",
            choices=choices,
            multiselect=True,
        ).execute_async()

        return selected
