import re

TEMP_PATH_TEMPLATE = "gamdl_temp_{}"
ILLEGAL_CHARS_RE = r'[\\/:*?"<>|;]'
ILLEGAL_CHAR_REPLACEMENT = "_"

SONG_MEDIA_TYPE = {"song", "songs", "library-songs"}
ALBUM_MEDIA_TYPE = {"album", "albums", "library-albums"}
MUSIC_VIDEO_MEDIA_TYPE = {"music-video", "music-videos", "library-music-videos"}
ARTIST_MEDIA_TYPE = {"artist", "artists", "library-artists"}
UPLOADED_VIDEO_MEDIA_TYPE = {"post", "uploaded-videos"}
PLAYLIST_MEDIA_TYPE = {"playlist", "playlists", "library-playlists"}

VALID_URL_PATTERN = re.compile(
    r"https://music\.apple\.com"
    r"(?:"
    r"/(?P<storefront>[a-z]{2})"
    r"/(?P<type>artist|album|playlist|song|music-video|post)"
    r"(?:/(?P<slug>[^\s/]+))?"
    r"/(?P<id>[0-9]+|pl\.[0-9a-z]{32}|pl\.u-[a-zA-Z0-9]+)"
    r"(?:\?i=(?P<sub_id>[0-9]+))?"
    r"|"
    r"(?:/(?P<library_storefront>[a-z]{2}))?"
    r"/library/(?P<library_type>playlist|albums)"
    r"/(?P<library_id>p\.[a-zA-Z0-9]+|l\.[a-zA-Z0-9]+)"
    r")"
)
