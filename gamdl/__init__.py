import shutil
import argparse
import traceback
from .gamdl import Gamdl

__version__ = '1.8'


def main():
    if not shutil.which('mp4decrypt'):
        raise Exception('mp4decrypt is not on PATH')
    if not shutil.which('MP4Box'):
        raise Exception('MP4Box is not on PATH')
    parser = argparse.ArgumentParser(
        description = 'Download Apple Music songs/music videos/albums/playlists',
        formatter_class = argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        'url',
        help = 'Apple Music song/music video/album/playlist URL(s)',
        nargs = '*',
    )
    parser.add_argument(
        '-u',
        '--urls-txt',
        help = 'Read URLs from a text file',
        nargs = '?',
    )
    parser.add_argument(
        '-w',
        '--wvd-location',
        default = '*.wvd',
        help = '.wvd file location'
    )
    parser.add_argument(
        '-f',
        '--final-path',
        default = 'Apple Music',
        help = 'Final Path',
    )
    parser.add_argument(
        '-t',
        '--temp-path',
        default = 'temp',
        help = 'Temp Path',
    )
    parser.add_argument(
        '-c',
        '--cookies-location',
        default = 'cookies.txt',
        help = 'Cookies location',
    )
    parser.add_argument(
        '-m',
        '--disable-music-video-skip',
        action = 'store_true',
        help = 'Disable music video skip on playlists/albums',
    )
    parser.add_argument(
        '-p',
        '--prefer-hevc',
        action = 'store_true',
        help = 'Prefer HEVC over AVC',
    )
    parser.add_argument(
        '-a',
        '--heaac',
        action = 'store_true',
        help = 'Download songs/music videos with HE-AAC instead of AAC',
    )
    parser.add_argument(
        '-o',
        '--overwrite',
        action = 'store_true',
        help = 'Overwrite existing files',
    )
    parser.add_argument(
        '-n',
        '--no-lrc',
        action = 'store_true',
        help = "Don't create .lrc file",
    )
    parser.add_argument(
        '-s',
        '--skip-cleanup',
        action = 'store_true',
        help = 'Skip cleanup',
    )
    parser.add_argument(
        '-e',
        '--print-exceptions',
        action = 'store_true',
        help = 'Print execeptions',
    )
    parser.add_argument(
        '-i',
        '--print-video-m3u8-url',
        action = 'store_true',
        help = 'Print Video M3U8 URL',
    )
    parser.add_argument(
        '-v',
        '--version',
        action = 'version',
        version = f'%(prog)s {__version__}',
    )
    args = parser.parse_args()
    if not args.url and not args.urls_txt:
        parser.error('you must specify an url or a text file using -u/--urls-txt')
    if args.urls_txt:
        with open(args.urls_txt, 'r', encoding = 'utf8') as f:
            args.url = f.read().splitlines()
    dl = Gamdl(
        args.wvd_location,
        args.cookies_location,
        args.disable_music_video_skip,
        args.prefer_hevc,
        args.heaac,
        args.temp_path,
        args.final_path,
        args.no_lrc,
        args.overwrite,
        args.skip_cleanup,
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
            print(f'Failed to check URL {i + 1}/{len(args.url)}')
            if args.print_exceptions:
                traceback.print_exc()
    for i, url in enumerate(download_queue):
        for j, track in enumerate(url):
            print(f'Downloading "{track["attributes"]["name"]}" (track {j + 1}/{len(url)} from URL {i + 1}/{len(download_queue)})')
            track_id = track['id']
            try:
                webplayback = dl.get_webplayback(track_id)
                if track['type'] == 'music-videos':
                    if args.print_video_m3u8_url:
                        print(webplayback['hls-playlist-url'])
                    tags = dl.get_tags_music_video(track['attributes']['url'].split('/')[-1].split('?')[0])
                    final_location = dl.get_final_location('.m4v', tags)
                    if dl.check_exists(final_location) and not args.overwrite:
                        continue
                    stream_url_video, stream_url_audio = dl.get_stream_url_music_video(webplayback)
                    decryption_keys_audio = dl.get_decryption_keys_music_video(stream_url_audio, track_id)
                    encrypted_location_audio = dl.get_encrypted_location_audio(track_id)
                    dl.download(encrypted_location_audio, stream_url_audio)
                    decrypted_location_audio = dl.get_decrypted_location_audio(track_id)
                    dl.decrypt(encrypted_location_audio, decrypted_location_audio, decryption_keys_audio)
                    decryption_keys_video = dl.get_decryption_keys_music_video(stream_url_video, track_id)
                    encrypted_location_video = dl.get_encrypted_location_video(track_id)
                    dl.download(encrypted_location_video, stream_url_video)
                    decrypted_location_video = dl.get_decrypted_location_video(track_id)
                    dl.decrypt(encrypted_location_video, decrypted_location_video, decryption_keys_video)
                    fixed_location = dl.get_fixed_location(track_id, '.m4v')
                    dl.fixup_music_video(decrypted_location_audio, decrypted_location_video, fixed_location)
                    dl.make_final(final_location, fixed_location, tags)
                else:
                    unsynced_lyrics, synced_lyrics = dl.get_lyrics(track_id)
                    tags = dl.get_tags_song(webplayback, unsynced_lyrics)
                    final_location = dl.get_final_location('.m4a', tags)
                    if dl.check_exists(final_location) and not args.overwrite:
                        continue
                    stream_url = dl.get_stream_url_song(webplayback)
                    decryption_keys = dl.get_decryption_keys_song(stream_url, track_id)
                    encrypted_location = dl.get_encrypted_location_audio(track_id)
                    dl.download(encrypted_location, stream_url)
                    decrypted_location = dl.get_decrypted_location_audio(track_id)
                    dl.decrypt(encrypted_location, decrypted_location, decryption_keys)
                    fixed_location = dl.get_fixed_location(track_id, '.m4a')
                    dl.fixup_song(decrypted_location, fixed_location)
                    dl.make_final(final_location, fixed_location, tags)
                    dl.make_lrc(final_location, synced_lyrics)
            except KeyboardInterrupt:
                exit(1)
            except:
                error_count += 1
                print(f'Failed to download "{track["attributes"]["name"]}" (track {j + 1}/{len(url)} from URL {i + 1}/{len(download_queue)})')
                if args.print_exceptions:
                    traceback.print_exc()
            dl.cleanup()
    print(f'Done ({error_count} error(s))')
