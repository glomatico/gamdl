# Changelog

## Unreleased

- Restore configurable cover artwork dimensions with separate `output.embedded_cover_size` and `output.file_cover_size` settings (both default to 1200 pixels), including CLI flags, playlist sync support, and `max` for Apple's maximum-resolution artwork URL (`10000x0w-999.<format>`).
- Fix a full-screen authentication wizard crash when switching from the last menu choice to a token or credential prompt.
- Restore music-video iTunes tag fallback by passing the lazily initialized iTunes API to the downloader's tag resolver.
- Publish downloads and sidecars through ordinary copies and renames instead of hard links to improve Windows and external-drive compatibility. Preserve no-overwrite behavior, temporary-file cleanup, and completed-file visibility where the filesystem supports exclusive renames.
- Organize files and sidecars without hard links, retaining originals until destination publication and database updates succeed.

## 4.0.0

Gamdl 4.0 is a major redesign of the CLI, Python API, download engine, configuration, and local-library tooling. It retains core capabilities from 3.9.1—including song, album, playlist, artist, music-video, uploaded-video, and Apple Music Classical downloads; multiple audio/video codecs; metadata; and synchronized lyrics—while substantially changing how they are resolved, downloaded, and managed.

**This release has breaking changes.** Read [Upgrading from 3.x](#upgrading-from-3x) before replacing an existing installation.

### New commands and discovery

- **Subcommand-based CLI.** `gamdl rip` handles one or more URLs in a shared authenticated session. Dedicated commands now cover `auth`, `config`, `library`, `search`, `database`, `organize`, and `sync playlists`.
- **Catalog search.** `gamdl search` browses Apple Music songs, music videos, albums, editorial playlists, and public user-shared playlists. Results are paginated and can be selected across media types; choosing an album or playlist expands its tracks. `--type` narrows the category, `--feeling-lucky` picks the first result when a type is specified, and `--search-api itunes` offers an alternative search backend.
- **ISRC inputs.** `gamdl rip isrc:<12-character-code>` resolves an ISRC against both Apple Music's song and music-video catalogs. One result proceeds directly; multiple returned results open a selection menu. Lookups are storefront-dependent and currently consider the returned catalog `data`, not every alternate release referenced by the API.
- **Personal library browser.** `gamdl library` browses saved albums, playlists, songs, and music videos, with `--library-auto-select` for unattended category selection.
- **Expanded artist browsing.** Artist pages have a hierarchical full-screen browser for album categories, top songs, and music videos, with on-demand paginated loading. The existing artist auto-selection feature remains available, now with explicit category options.

### Download and media processing

- **Rebuilt native download pipeline.** A Rust-backed streamer now handles concurrent HLS media-segment requests, byte ranges, retries, progressive HTTP assets, and native progress reporting. This replaces 3.x's yt-dlp / N_m3u8DL-RE downloader modes for normal downloads.
- **Stream-aware decryption.** Media discovery and download coordinate Widevine/PlayReady keys, CENC and CBCS decryption, and Wrapper v2 FairPlay sample delivery. Compatible wrapper playback/decryption is preferred where applicable, with other streams routed through the configured DRM backend rather than assuming wrapper support for everything.
- **File-backed native processing and safer publication.** Building on the native MP4 operations already present in 3.x, large downloads and mux operations now use staged files and bounded buffering instead of retaining entire media files in memory. The revised muxer preserves supported caption tracks; final outputs are published only after processing, and tagging preserves file permissions while avoiding unnecessary full-payload copies for supported MP4 layouts.
- **Automatic ALAC packet repair.** During native audio remuxing, Gamdl detects supported malformed ALAC packet trailers, including missing `TYPE_END` terminators, and repairs them without audio re-encoding. Valid packets are left untouched; packets whose boundaries cannot be identified safely are not guessed at. This is enabled by default for the native remuxer and can be disabled in the Python API with `Muxer(repair_alac=False)`.
- **Native metadata writing.** MP4/M4A/M4V tags and artwork are written through the native extension; native ID3v2 tagging also supports uploaded MP3 song assets. The tag resolver draws on playback metadata and limited iTunes enrichment where appropriate.
- **Better handling of existing outputs.** With the database enabled and overwrite disabled, matching catalog IDs can be reused before unnecessary per-track metadata, playback, or license requests. Missing sidecars can be added without downloading the media again. Explicit codec requirements and overwrite requests still trigger the necessary checks.

### Codec and format selection

- **Default song codec changed to HLS AAC (`aac`).** The previous `aac-web` default is still available explicitly. HLS and web-playback streams remain distinct and are selected according to the chosen codec and available authentication/decryption methods.
- **Explicit ALAC quality tiers.** Automatic selection of the best ALAC stream already existed in 3.x. Version 4.0 additionally distinguishes `alac-hires` and `alac-lossless` as explicit codec choices and records the resolved tier rather than only the generic `alac` preference.
- **Separate exact-codec and priority settings.** Song and music-video codecs can be specified as an exact choice or ordered fallback list. Interactive choices use `--ask-song-codec` and `--ask-music-video-streams` instead of a special `ask` codec value.
- **Music-video stream selection improvements.** H.264 and H.265 renditions are selected using advertised manifest resolutions and bitrates, subject to `--max-height`. Automatic audio selection prefers the highest-bitrate AAC rendition, then HE-AAC; surround formats can be chosen manually when available. A high-resolution DRM security-level warning helps explain potential 4K limitations.
- **Choice of music-video container.** `--music-video-container m4v|mp4` controls the extension and compatible MP4 brand while keeping streams lossless. Both container choices existed in 3.x; this is their 4.0 configuration interface.
- **Uploaded-video quality selection.** Known Apple Music asset variants can be chosen directly or through `--ask-uploaded-video-quality`, preserving the ability to download post/uploaded videos.

### Lyrics and metadata

- **Independent external and embedded lyrics formats.** `output.lyrics_file_format` and `output.embedded_lyrics_format` (or their respective CLI flags) support `lrc`, `enhanced-lrc`, `srt`, `ttml`, and `text`. Defaults are line-timed LRC for external files and plain text for the `©lyr` embedded tag.
- **Enhanced LRC.** Syllable/word timing is emitted when Apple provides it; otherwise enhanced-LRC requests fall back to ordinary line-timed LRC. SRT conversion can also use suitable line timings from syllable-synchronized lyrics.
- **Predictable sidecars.** If the requested external format cannot be produced, Gamdl does not silently substitute another extension (apart from the enhanced-LRC-to-LRC fallback). Existing sidecars are checked by selected format and can be repaired or created independently of the media file.
- **Independent lyrics controls.** `--no-lyrics-file` suppresses external lyrics only. `--exclude-tags lyrics` or `--no-tag` suppresses embedded lyrics; `--exclude-tags` continues to support fine-grained omission of other metadata and artwork.
- **Metadata robustness.** Fixes cover large Apple catalog identifiers, playback-derived codec-specific tags, chapter preservation, album/date fallback handling, filename sanitization, and existing-file permissions. JPEG and PNG artwork remain supported.

### Local library, database, and playlists

- **Reworked SQLite media index.** The index is enabled by default, uses portable paths relative to the output library, records media identity and source provenance, and optionally stores resolved song/music-video/uploaded-video metadata via `database.save_metadata`.
- **Database maintenance.** `gamdl database info`, `database scan`, and `database prune` inspect the index, reconstruct local media membership from embedded identifiers and local album/playlist structures, and remove stale _database records_. Scanning is local-only; pruning never deletes media files. Dry-run modes are available.
- **Offline library organization.** `gamdl organize --dry-run` previews moves based on embedded metadata and path templates; `gamdl organize` applies safe moves, including associated cover/lyrics files and database references. Ambiguous/insufficiently tagged media, unsafe paths, and conflicting destinations are skipped rather than overwritten.
- **Playlist path repair.** The organizer updates relative paths in existing `.m3u8` playlists after media moves, without renaming playlist files or rewriting their title/header metadata. `--playlists-only` repairs references without relocating media files.
- **Playlist synchronization.** `gamdl sync playlists` compares local M3U playlists with their Apple Music catalog playlists, tracking additions, removals, and order changes. It identifies and downloads unique missing tracks using the configured codecs and concurrent progress display; `--no-download`, `--dry-run`, `--verbose`, and individual playlist selection are supported.
- **Conservative synchronization.** Removed playlist entries do **not** delete downloaded songs/videos. Duplicate track occurrences and ordering are preserved. Failed downloads or incomplete remote responses do not publish partial changes to affected playlists; the operation also preserves existing playlist filenames and metadata headers.

### Configuration, authentication, and interface

- **YAML configuration.** A typed, versioned configuration replaces the 3.x INI file. Settings are grouped under `auth`, `download`, `output`, `database`, `ui`, and `search`; `gamdl config init|show|validate|path` provides setup and inspection. Configuration normally lives in `~/.gamdl/config.yml`, with CLI and environment overrides.
- **Guided first run.** An authentication wizard can configure Wrapper, exported Apple Music cookies, or a direct media-user-token login. The first download can also choose the OS user Music directory and save that choice.
- **Optional DRM dependencies.** Widevine and PlayReady were already supported in 3.x, including the bundled Widevine L3 device and custom PlayReady devices. In 4.0, their Python packages can be installed separately via `gamdl[widevine]` and `gamdl[playready]` instead of being mandatory base dependencies.
- **Full-screen interactive UI.** Rich progress bars and Prompt Toolkit selectors provide per-track download stages, codec/quality choices, paginated search and artist browsing, and bounded visible rows.
- **Explicit concurrency controls.** `download.concurrent_downloads` controls simultaneous media items (default `2`); `download.segment_concurrency` controls parallel segment requests **per item** (default `8`). The two limits are independent.
- **Error handling.** Downloads continue past individual failures unless `--fail-fast` is requested. Expected unavailability is reported concisely; unexpected failures can be displayed with tracebacks after the progress UI closes, or suppressed with `--no-tracebacks`.
- **Safer HTTP handling.** Shared API request pacing and retry behavior, pagination guards, and isolated public-CDN transports reduce accidental authentication-header leakage across redirects.

### Python API and distribution

- **Reorganized asynchronous API.** Lazy media classes and `MediaResolver` support direct resources, albums, playlists, artists, and library content. Separate `AppleMusicApi`, `ItunesApi`, `WrapperApi`, `Drm`, `Streamer`, `Muxer`, and `Downloader` interfaces replace the old 3.x interface/downloader class layout.
- **Native extension renamed and expanded.** The PyO3 extension is now `gamdl._native` rather than `gamdl._ammuxer`, covering streaming, decryption, remuxing, muxing, and tagging.
- **Cross-platform release workflow prepared.** The release pipeline is configured to build a source archive plus Python 3.10+ ABI3 wheels for Linux x86_64/ARM64, Windows x86_64/ARM64, and macOS universal2. It performs wheel install/smoke checks and uses PyPI Trusted Publishing on a published GitHub Release; manual workflow runs build artifacts without publishing. **These platform-specific GitHub Actions builds still need a successful run before the release is considered verified.**

### Fixes and reliability work

- Improved CBCS sample-pattern handling, subsample IV/cipher chaining, key identification, wrapper sample delivery, and rejection of undeciphered ciphertext instead of producing deceptively valid files.
- Improved HLS manifest handling, stream-source fallback, unsupported and non-streamable media detection, DRM security warnings, and missing-lyrics behavior.
- Fixed fragmented MP4 sample-description normalization and track enablement; avoided mux output inconsistencies that could affect playback compatibility.
- Reduced memory usage for large HLS downloads, MP4 parsing, file-backed muxing, and MP4 tagging; isolated public media downloads from sensitive API sessions.
- Hardened path generation (including filenames beginning with dots), playlist references, same-name video collisions, temporary filenames, and safe handling of symlinked or occupied destinations.
- Expanded automated coverage across Python 3.10–3.14, Rust native code, offline API behavior, and opt-in live integration tests.

### Upgrading from 3.x

**Existing 3.x configuration and integration code are not drop-in compatible.**

| Gamdl 3.x | Gamdl 4.0 |
| --- | --- |
| `gamdl URL...` | `gamdl rip URL...` |
| `~/.gamdl/config.ini` | `~/.gamdl/config.yml` (YAML; **not** automatic INI conversion) |
| `--config-path` / `--no-config-file` | `--config` / `--no-config` |
| `--use-wrapper` | `gamdl auth` or `auth.mode: wrapper` |
| `--wvd-path` / `--prd-path` | `--device-path`, with `--drm-backend` as appropriate |
| `--output-path` | `--output-dir` |
| `--song-codec-priority ...` with default `aac-web` | Default `aac` (HLS); `aac-web` remains an explicit codec |
| `--song-codec-priority ask` / `--music-video-codec-priority ask` | `--ask-song-codec` / `--ask-music-video-streams` |
| `--synced-lyrics-format` | `--lyrics-file-format` **and** `--embedded-lyrics-format` |
| `--no-synced-lyrics` / `--synced-lyrics-only` | `--no-lyrics-file` affects sidecars; there is no direct equivalent of the old lyrics-only download mode |
| `--music-video-resolution` | `--max-height` (numeric pixels) |
| `--music-video-remux-format` | `--music-video-container` |
| `--download-mode` (`ytdlp` / `nm3u8dlre`) | Removed; the internal native streamer is used |
| `--exclude-tags all` | `--no-tag`; `all` is no longer an accepted tag exclusion |
| Old `gamdl.interface` / `gamdl.downloader` classes, `_ammuxer` | New asynchronous media/download APIs and `gamdl._native` |

- **Database:** The 4.0 SQLite schema is incompatible with the old index. Do not point 4.0 at an existing 3.x database expecting automatic migration; create a fresh index or choose a different database path. Downloaded media itself does not need to be deleted.
- **Default paths:** Output now prompts for the user's OS Music directory on first setup instead of automatically using `./Apple Music`; confirm the destination and path templates before downloading.
- **Installation:** Python 3.10+ remains the minimum. Install the appropriate DRM extra (`gamdl[widevine]` or `gamdl[playready]`) if protected playback needs that backend. Release binaries are intended to avoid requiring a local Rust build on supported platforms.
- **Other removed flags:** Do not assume old download-mode, logging, URL-text-file, cover-size, or other 3.x switches still work. Review `gamdl --help`, `gamdl rip --help`, and `gamdl config show` when migrating scripts.
- **Existing libraries:** Run `gamdl organize --dry-run` and `gamdl sync playlists --dry-run` before modifying previously downloaded collections.

### Notes

- Availability of codecs, ALAC tiers, lyrics timing, video heights, and DRM licenses depends on the media, storefront, account, and stream origin.
- Synchronized lyrics embedded into `©lyr` remain text metadata; playback applications may display timing markup literally rather than implement synchronized highlighting.
- ISRC lookups may return one or several catalog entries, or none in the selected storefront; they do not search a global recording database.
- This document describes functionality present on the **4.0 branch**, compared with the **3.9.1 release**. It does not include exploratory features or commands developed only on other branches.
