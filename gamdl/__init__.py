import argparse
import shutil
import traceback

from .gamdl import Gamdl

__version__ = "1.9.3"


def main():
    for tool in ("MP4Box", "mp4decrypt"):
        if not shutil.which(tool):
            raise Exception(f"{tool} is not on PATH")
    parser = argparse.ArgumentParser(
        description="Download Apple Music songs/music videos/albums/playlists",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "url",
        help="Apple Music song/music video/album/playlist URL(s)",
        nargs="+",
    )
    parser.add_argument(
        "-u",
        "--urls-txt",
        help="Read URLs from a text file",
        action="store_true",
    )
    parser.add_argument(
        "-w",
        "--wvd-location",
        default="./*.wvd",
        help=".wvd file location (ignored if using -l/--lrc-only)",
    )
    parser.add_argument(
        "-f",
        "--final-path",
        default="./Apple Music",
        help="Final Path",
    )
    parser.add_argument(
        "-t",
        "--temp-path",
        default="./temp",
        help="Temp Path",
    )
    parser.add_argument(
        "-c",
        "--cookies-location",
        default="./cookies.txt",
        help="Cookies location",
    )
    parser.add_argument(
        "-m",
        "--disable-music-video-skip",
        action="store_true",
        help="Disable music video skip on playlists/albums",
    )
    parser.add_argument(
        "-p",
        "--prefer-hevc",
        action="store_true",
        help="Prefer HEVC over AVC",
    )
    parser.add_argument(
        "-o",
        "--overwrite",
        action="store_true",
        help="Overwrite existing files",
    )
    parser.add_argument(
        "-n",
        "--no-lrc",
        action="store_true",
        help="Don't create .lrc file (ignored if using -l/--lrc-only)",
    )
    parser.add_argument(
        "-l",
        "--lrc-only",
        action="store_true",
        help="Skip downloading songs and only create .lrc files",
    )
    parser.add_argument(
        "-e",
        "--print-exceptions",
        action="store_true",
        help="Print execeptions",
    )
    parser.add_argument(
        "-v",
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    args = parser.parse_args()
    if args.urls_txt:
        _url = []
        for url_txt in args.url:
            with open(url_txt, "r", encoding="utf8") as f:
                _url.extend(f.read().splitlines())
        args.url = _url
    dl = Gamdl(
        args.wvd_location,
        args.cookies_location,
        args.disable_music_video_skip,
        args.prefer_hevc,
        args.temp_path,
        args.final_path,
        args.lrc_only,
        args.overwrite,
    )
    error_count = 0
    download_queue = []
    for i, url in enumerate(args.url):
        try:
            download_queue.append(dl.get_download_queue(url.strip()))
        except KeyboardInterrupt:
            exit(1)
        except:
            error_count += 1
            print(f"Failed to check URL {i + 1}/{len(args.url)}")
            if args.print_exceptions:
                traceback.print_exc()
    for i, url in enumerate(download_queue):
        for j, track in enumerate(url):
            print(
                f'Downloading "{track["attributes"]["name"]}" (track {j + 1}/{len(url)} from URL {i + 1}/{len(download_queue)})'
            )
            track_id = track["id"]
            try:
                webplayback = dl.get_webplayback(track_id)
                if track["type"] == "music-videos":
                    tags = dl.get_tags_music_video(
                        track["attributes"]["url"].split("/")[-1].split("?")[0]
                    )
                    final_location = dl.get_final_location(".m4v", tags)
                    if final_location.exists() and not args.overwrite:
                        continue
                    stream_url_video, stream_url_audio = dl.get_stream_url_music_video(
                        webplayback
                    )
                    decryption_keys_audio = dl.get_decryption_keys_music_video(
                        stream_url_audio, track_id
                    )
                    encrypted_location_audio = dl.get_encrypted_location_audio(track_id)
                    dl.download(encrypted_location_audio, stream_url_audio)
                    decrypted_location_audio = dl.get_decrypted_location_audio(track_id)
                    dl.decrypt(
                        encrypted_location_audio,
                        decrypted_location_audio,
                        decryption_keys_audio,
                    )
                    decryption_keys_video = dl.get_decryption_keys_music_video(
                        stream_url_video, track_id
                    )
                    encrypted_location_video = dl.get_encrypted_location_video(track_id)
                    dl.download(encrypted_location_video, stream_url_video)
                    decrypted_location_video = dl.get_decrypted_location_video(track_id)
                    dl.decrypt(
                        encrypted_location_video,
                        decrypted_location_video,
                        decryption_keys_video,
                    )
                    fixed_location = dl.get_fixed_location(track_id, ".m4v")
                    dl.fixup_music_video(
                        decrypted_location_audio,
                        decrypted_location_video,
                        fixed_location,
                    )
                    final_location.parent.mkdir(parents=True, exist_ok=True)
                    dl.move_final(final_location, fixed_location, tags)
                else:
                    unsynced_lyrics, synced_lyrics = dl.get_lyrics(track_id)
                    tags = dl.get_tags_song(webplayback, unsynced_lyrics)
                    final_location = dl.get_final_location(".m4a", tags)
                    if args.lrc_only:
                        final_location.parent.mkdir(parents=True, exist_ok=True)
                        dl.make_lrc(final_location, synced_lyrics)
                        continue
                    if final_location.exists() and not args.overwrite:
                        continue
                    stream_url = dl.get_stream_url_song(webplayback)
                    decryption_keys = dl.get_decryption_keys_song(stream_url, track_id)
                    encrypted_location = dl.get_encrypted_location_audio(track_id)
                    dl.download(encrypted_location, stream_url)
                    decrypted_location = dl.get_decrypted_location_audio(track_id)
                    dl.decrypt(encrypted_location, decrypted_location, decryption_keys)
                    fixed_location = dl.get_fixed_location(track_id, ".m4a")
                    dl.fixup_song(decrypted_location, fixed_location)
                    final_location.parent.mkdir(parents=True, exist_ok=True)
                    dl.move_final(final_location, fixed_location, tags)
                    if not args.no_lrc:
                        dl.make_lrc(final_location, synced_lyrics)
            except KeyboardInterrupt:
                exit(1)
            except:
                error_count += 1
                print(
                    f'Failed to download "{track["attributes"]["name"]}" (track {j + 1}/{len(url)} from URL {i + 1}/{len(download_queue)})'
                )
                if args.print_exceptions:
                    traceback.print_exc()
            dl.cleanup()
    print(f"Done ({error_count} error(s))")
