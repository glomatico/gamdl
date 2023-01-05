from pathlib import Path
import datetime
import argparse
import shutil
import traceback
import subprocess
import re
from xml.dom import minidom
import base64
from pywidevine.L3.cdm.cdm import Cdm
from pywidevine.L3.cdm import deviceconfig
from pywidevine.L3.cdm.formats.widevine_pssh_data_pb2 import WidevinePsshData
import requests
import storefront_ids
import m3u8
import urllib3
from yt_dlp import YoutubeDL
from mutagen.mp4 import MP4Cover, MP4
import song_genres
import music_video_genres

class Gamdl:
    def __init__(self, disable_music_video_skip, cookies_location, temp_path, prefer_hevc, final_path, skip_cleanup, print_video_playlist, no_lrc):
        self.cdm = Cdm()
        self.disable_music_video_skip = disable_music_video_skip
        self.cookies_location = Path(cookies_location)
        self.temp_path = Path(temp_path)
        self.prefer_hevc = prefer_hevc
        self.final_path = Path(final_path)
        self.skip_cleanup = skip_cleanup
        self.print_video_playlist = print_video_playlist
        self.no_lrc = no_lrc
        cookies = {}
        with open(self.cookies_location, 'r') as f:
            for l in f:
                if not re.match(r"^#", l) and not re.match(r"^\n", l):
                    line_fields = l.strip().replace('&quot;', '"').split('\t')
                    cookies[line_fields[5]] = line_fields[6]
        self.session = requests.Session()
        self.session.verify = False
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        self.session.cookies.update(cookies)
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:95.0) Gecko/20100101 Firefox/95.0",
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.5",
            "Accept-Encoding": "gzip, deflate, br",
            "content-type": "application/json",
            "Media-User-Token": self.session.cookies.get_dict()["media-user-token"],
            "x-apple-renewal": "true",
            "DNT": "1",
            "Connection": "keep-alive",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-site",
            'origin': 'https://beta.music.apple.com'
        })
        r = self.session.get('https://beta.music.apple.com')
        index_js = re.search('(?<=index\.)(.*?)(?=\.js")', r.text).group(1)
        r = self.session.get(f'https://beta.music.apple.com/assets/index.{index_js}.js')
        access_token = re.search('(?=eyJh)(.*?)(?=")', r.text).group(1)
        self.session.headers.update({"authorization": f'Bearer {access_token}'})
        self.country = cookies['itua'].lower()
        self.storefront = getattr(storefront_ids, self.country.upper())
    

    def get_download_queue(self, url):
        download_queue = []
        product_id = url.split('/')[-1].split('i=')[-1].split('&')[0].split('?')[0]
        response = self.session.get(f'https://api.music.apple.com/v1/catalog/{self.country}/?ids[songs]={product_id}&ids[albums]={product_id}&ids[playlists]={product_id}&ids[music-videos]={product_id}').json()['data'][0]
        if response['type'] == 'songs':
            download_queue.append({
                'track_id': response['id'],
                'title': response['attributes']['name']
            })
        if response['type'] == 'albums' or response['type'] == 'playlists':
            for track in response['relationships']['tracks']['data']:
                if 'playParams' in track['attributes']:
                    if track['type'] == 'music-videos' and self.disable_music_video_skip:
                        download_queue.append({
                            'track_id': track['attributes']['playParams']['id'],
                            'alt_track_id': track['attributes']['url'].split('/')[-1],
                            'title': track['attributes']['name']
                        })
                    if track['type'] == 'songs':
                        download_queue.append({
                            'track_id': track['attributes']['playParams']['id'],
                            'title': track['attributes']['name']
                        })
        if response['type'] == 'music-videos':
            download_queue.append({
                'track_id': response['attributes']['playParams']['id'],
                'alt_track_id': response['attributes']['url'].split('/')[-1],
                'title': response['attributes']['name']
            })
        if not download_queue:
            raise Exception()
        return download_queue
    

    def get_webplayback(self, track_id):
        response = self.session.post(
            'https://play.itunes.apple.com/WebObjects/MZPlay.woa/wa/webPlayback',
            json = {
                'salableAdamId': track_id
            }
        ).json()["songList"][0]
        return response
    

    def get_playlist_music_video(self, webplayback):
        playlist = m3u8.load(webplayback['hls-playlist-url'])
        if self.print_video_playlist:
            print(playlist.dumps())
        return playlist
    

    def get_stream_url_song(self, webplayback):
        return next((x for x in webplayback["assets"] if x["flavor"] == "28:ctrp256"))['URL']
    

    def get_stream_url_music_video_audio(self, playlist):
        return [x for x in playlist.media if x.type == "AUDIO"][-1].uri
    

    def get_stream_url_music_video_video(self, playlist):
        if self.prefer_hevc:
            return playlist.playlists[-1].uri
        else:
            return [x for x in playlist.playlists if 'avc' in x.stream_info.codecs][-1].uri
    
    
    def get_encrypted_location(self, extension, track_id,):
        return self.temp_path / f'{track_id}_encrypted{extension}'
    

    def get_decrypted_location(self, extension, track_id):
        return self.temp_path / f'{track_id}_decrypted{extension}'
    

    def get_fixed_location(self, extension, track_id):
        return self.temp_path / f'{track_id}_fixed{extension}'
    

    def download(self, encrypted_location, stream_url):
        with YoutubeDL({
            'quiet': True,
            'no_warnings': True,
            'outtmpl': str(encrypted_location),
            'allow_unplayable_formats': True,
            'fixup': 'never',
            'overwrites': True,
            'external_downloader': 'aria2c'
        }) as ydl:
            ydl.download(stream_url)
        
    
    def get_license_b64(self, challenge, track_uri, track_id):
        return self.session.post(
            'https://play.itunes.apple.com/WebObjects/MZPlay.woa/wa/acquireWebPlaybackLicense',
            json = {
                'challenge': challenge,
                'key-system': 'com.widevine.alpha',
                'uri': track_uri,
                'adamId': track_id,
                'isLibrary': False,
                'user-initiated': True
            }
        ).json()['license']
    

    def check_pssh(self, pssh_b64):
        WV_SYSTEM_ID = [237, 239, 139, 169, 121, 214, 74, 206, 163, 200, 39, 220, 213, 29, 33, 237]
        pssh = base64.b64decode(pssh_b64)
        if not pssh[12:28] == bytes(WV_SYSTEM_ID):
            new_pssh = bytearray([0, 0, 0])
            new_pssh.append(32 + len(pssh))
            new_pssh[4:] = bytearray(b'pssh')
            new_pssh[8:] = [0, 0, 0, 0]
            new_pssh[13:] = WV_SYSTEM_ID
            new_pssh[29:] = [0, 0, 0, 0]
            new_pssh[31] = len(pssh)
            new_pssh[32:] = pssh
            return base64.b64encode(new_pssh)
        else:
            return pssh_b64
    
    
    def get_decryption_keys_music_video(self, stream_url, track_id):
        playlist = m3u8.load(stream_url)
        track_uri = next(x for x in playlist.keys if x.keyformat == "urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed").uri
        session = self.cdm.open_session(
            self.check_pssh(track_uri.split(',')[1]),
            deviceconfig.DeviceConfig(deviceconfig.device_android_generic)
        )
        challenge = base64.b64encode(self.cdm.get_license_request(session)).decode('utf-8')
        license_b64 = self.get_license_b64(challenge, track_uri, track_id)
        self.cdm.provide_license(session, license_b64)
        decryption_keys = []
        for key in self.cdm.get_keys(session):
            if key.type == 'CONTENT':
                decryption_keys.append(f'1:{key.key.hex()}')
        return decryption_keys[0]

    
    def get_decryption_keys_song(self, stream_url, track_id):
        track_uri = m3u8.load(stream_url).keys[0].uri
        wvpsshdata = WidevinePsshData()
        wvpsshdata.algorithm = 1
        wvpsshdata.key_id.append(base64.b64decode(track_uri.split(",")[1]))
        session = self.cdm.open_session(
            self.check_pssh(base64.b64encode(wvpsshdata.SerializeToString()).decode("utf-8")),
            deviceconfig.DeviceConfig(deviceconfig.device_android_generic)
        )
        challenge = base64.b64encode(self.cdm.get_license_request(session)).decode('utf-8')
        license_b64 = self.get_license_b64(challenge, track_uri, track_id)
        self.cdm.provide_license(session, license_b64)
        decryption_keys = []
        for key in self.cdm.get_keys(session):
            if key.type == 'CONTENT':
                decryption_keys.append(f'{key.kid.hex()}:{key.key.hex()}')
        return decryption_keys[0]
    

    def decrypt(self, encrypted_location, decrypted_location, decryption_keys):
        subprocess.check_output([
            'mp4decrypt',
            encrypted_location,
            '--key',
            decryption_keys,
            decrypted_location
        ])
    

    def get_synced_lyrics_formated_time(self, unformatted_time):
        if 's' in unformatted_time:
            unformatted_time = unformatted_time.replace('s', '')
        if '.' not in unformatted_time:
            unformatted_time += '.0'
        s = int(unformatted_time.split('.')[-2].split(':')[-1]) * 1000
        try:
            m = int(unformatted_time.split('.')[-2].split(':')[-2]) * 60000
        except:
            m = 0
        ms = f'{int(unformatted_time.split(".")[-1]):03d}'
        if int(ms[2]) >= 5:
            ms = int(f'{int(ms[:2]) + 1}') * 10
        else:
            ms = int(ms)
        formated_time = datetime.datetime.fromtimestamp((s + m + ms)/1000.0)
        return formated_time.strftime('%M:%S.%f')[:-4]


    def get_lyrics(self, track_id):
        try:
            raw_lyrics = minidom.parseString(self.session.get(f'https://amp-api.music.apple.com/v1/catalog/{self.country}/songs/{track_id}/lyrics').json()['data'][0]['attributes']['ttml'])
        except:
            return
        unsynced_lyrics = ''
        synced_lyrics = ''
        for stanza in raw_lyrics.getElementsByTagName("div"):
            for verse in stanza.getElementsByTagName("p"):
                if not verse.firstChild.nodeValue:
                    subverse_time = []
                    subverse_text = []
                    for subserve in verse.getElementsByTagName("span"):
                        if subserve.firstChild.nodeValue:
                            subverse_time.append(subserve.getAttribute('begin'))
                            subverse_text.append(subserve.firstChild.nodeValue)
                    subverse_time = subverse_time[0]
                    subverse_text = ' '.join(subverse_text)
                    unsynced_lyrics += subverse_text + '\n'
                    if subverse_time:
                        synced_lyrics += f'[{self.get_synced_lyrics_formated_time(subverse_time)}]{subverse_text}\n'
                else:
                    unsynced_lyrics += verse.firstChild.nodeValue + '\n'
                    if verse.getAttribute('begin'):
                        synced_lyrics += f'[{self.get_synced_lyrics_formated_time(verse.getAttribute("begin"))}]{verse.firstChild.nodeValue}\n'
            unsynced_lyrics += '\n'
        return [unsynced_lyrics[:-2], synced_lyrics]
    

    def get_tags_song(self, webplayback, lyrics):
        metadata = next((x for x in webplayback["assets"] if x["flavor"] == "28:ctrp256"))['metadata']
        artwork_url = next((x for x in webplayback["assets"] if x["flavor"] == "28:ctrp256"))['artworkURL']
        tags = {
            '\xa9nam': [metadata['itemName']],
            '\xa9gen': [getattr(song_genres, f'ID{metadata["genreId"]}')],
            'aART': [metadata['playlistArtistName']],
            '\xa9alb': [metadata['playlistName']],
            'soar': [metadata['sort-artist']],
            'soal': [metadata['sort-album']],
            'sonm': [metadata['sort-name']],
            '\xa9ART': [metadata['artistName']],
            'geID': [metadata['genreId']],
            'atID': [int(metadata['artistId'])],
            'plID': [int(metadata['playlistId'])],
            'cnID': [int(metadata['itemId'])],
            'sfID': [metadata['s']],
            'rtng': [metadata['explicit']],
            'pgap': metadata['gapless'],
            'cpil': metadata['compilation'],
            'disk': [(metadata['discNumber'], metadata['discCount'])],
            'trkn': [(metadata['trackNumber'], metadata['trackCount'])],
            'covr': [MP4Cover(requests.get(artwork_url).content, MP4Cover.FORMAT_JPEG)],
            'stik': [1]
        }
        if 'copyright' in metadata:
            tags['cprt'] = [metadata['copyright']]
        if 'releaseDate' in metadata:
            tags['\xa9day'] = [metadata['releaseDate']]
        if 'comments' in metadata:
            tags['\xa9cmt'] = [metadata['comments']]
        if 'xid' in metadata:
            tags['xid '] = [metadata['xid']]
        if 'composerId' in metadata:
            tags['cmID'] = [int(metadata['composerId'])]
            tags['\xa9wrt'] = [metadata['composerName']]
            tags['soco'] = [metadata['sort-composer']]
        if lyrics:
            tags['\xa9lyr'] = [lyrics[0]]
        return tags
    

    def get_tags_music_video(self, track_id):
        metadata = requests.get(f'https://itunes.apple.com/lookup?id={track_id}&entity=album&limit=200&country={self.country}').json()['results']
        extra_metadata = requests.get(f'https://music.apple.com/music-video/{metadata[0]["trackId"]}', headers = {'X-Apple-Store-Front': f'{self.storefront} t:music31'}).json()['storePlatformData']['product-dv']['results'][str(metadata[0]['trackId'])]
        tags = {
            '\xa9ART': [metadata[0]["artistName"]],
            '\xa9nam': [metadata[0]["trackCensoredName"]],
            '\xa9day': [metadata[0]["releaseDate"]],
            'cprt': [extra_metadata['copyright']],
            '\xa9gen': [getattr(music_video_genres, f'ID{extra_metadata["genres"][0]["genreId"]}')],
            'stik': [6],
            'atID': [metadata[0]['artistId']],
            'cnID': [metadata[0]["trackId"]],
            'geID': [int(extra_metadata['genres'][0]['genreId'])],
            'sfID': [int(self.storefront.split('-')[0])],
            'covr': [MP4Cover(requests.get(metadata[0]["artworkUrl30"].replace('30x30bb.jpg', '600x600bb.jpg')).content, MP4Cover.FORMAT_JPEG)]
        }
        if metadata[0]['trackExplicitness'] == 'notExplicit':
            tags['rtng'] = [0]
        elif metadata[0]['trackExplicitness'] == 'explicit':
            tags['rtng'] = [1]
        else:
            tags['rtng'] = [2]
        if len(metadata) > 1:
            tags['\xa9alb'] = [metadata[1]["collectionCensoredName"]]
            tags['aART'] = [metadata[1]["artistName"]]
            tags['plID'] = [metadata[1]["collectionId"]]
            tags['disk'] = [(metadata[0]["discNumber"], metadata[0]["discCount"])]
            tags['trkn'] = [(metadata[0]["trackNumber"], metadata[0]["trackCount"])]
        return tags
    

    def get_sanizated_string(self, dirty_string, is_folder):
        for character in ['\\', '/', ':', '*', '?', '"', '<', '>', '|', ';']:
            dirty_string = dirty_string.replace(character, '_')
        if is_folder:
            dirty_string = dirty_string[:40]
            if dirty_string[-1:] == '.':
                dirty_string = dirty_string[:-1] + '_'
        else:
            dirty_string = dirty_string[:36]
        return dirty_string.strip()
    

    def get_final_location_overwrite_prevented_music_video(self, final_location):
        count = 1
        while True:
            if final_location.with_name(f'{final_location.stem} {count}.m4v').exists():
                count += 1
            else:
                return final_location.with_name(f'{final_location.stem} {count}.m4v')
    

    def get_final_location(self, file_extension, tags):
        final_location = self.final_path
        if 'plID' in tags:
            if tags['disk'][0][1] > 1:
                file_name = self.get_sanizated_string(f'{tags["disk"][0][0]}-{tags["trkn"][0][0]:02d} {tags["©nam"][0]}', False)
            else:
                file_name = self.get_sanizated_string(f'{tags["trkn"][0][0]:02d} {tags["©nam"][0]}', False)
            if 'cpil' in tags and tags['cpil']:
                final_location /= f'Compilations/{self.get_sanizated_string(tags["©alb"][0], True)}'
            else:
                final_location /= f'{self.get_sanizated_string(tags["aART"][0], True)}/{self.get_sanizated_string(tags["©alb"][0], True)}'
        else:
            file_name = self.get_sanizated_string(tags["©nam"][0], False)
            final_location /= f'{self.get_sanizated_string(tags["©ART"][0], True)}/Unknown Album/'
        final_location /= f'{file_name}{file_extension}'
        try:
            if final_location.exists() and file_extension == '.m4v' and MP4(final_location).tags['cnID'][0] != tags['cnID'][0]:
                final_location = self.get_final_location_overwrite_prevented_music_video(final_location)
        except:
            pass
        return final_location
    

    def fixup_music_video(self, decrypted_location_audio, decrypted_location_video, fixed_location):
        subprocess.check_output([
            'MP4Box',
            '-quiet',
            '-add',
            decrypted_location_audio,
            '-add',
            decrypted_location_video,
            '-itags',
            'artist=placeholder',
            '-new',
            fixed_location
        ])
    

    def fixup_song(self, decrypted_location, fixed_location):
        subprocess.check_output([
            'MP4Box',
            '-quiet',
            '-add',
            decrypted_location,
            '-itags',
            'album=placeholder',
            '-new',
            fixed_location
        ])
    

    def make_lrc(self, final_location, lyrics):
        if lyrics and lyrics[1] and not self.no_lrc:
            with open(final_location.with_suffix('.lrc'), 'w', encoding = 'utf8') as f:
                f.write(lyrics[1])
    

    def make_final(self, final_location, fixed_location, tags):
        final_location.parent.mkdir(parents = True, exist_ok = True)
        shutil.copy(fixed_location, final_location)
        file = MP4(final_location).tags
        for key, value in tags.items():
            file[key] = value
        file.save(final_location)
    

    def cleanup(self):
        if self.temp_path.exists() and not self.skip_cleanup:
            shutil.rmtree(self.temp_path)
    

if __name__ == '__main__':
    if not shutil.which('mp4decrypt'):
        raise Exception('mp4decrypt is not on PATH.')
    if not shutil.which('MP4Box'):
        raise Exception('MP4Box is not on PATH.')
    parser = argparse.ArgumentParser(
        description = 'A Python script to download Apple Music songs/music videos/albums/playlists.',
        formatter_class = argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument(
        'url',
        help='Apple Music song/music video/album/playlist URL(s)',
        nargs='*'
    )
    parser.add_argument(
        '-u',
        '--urls-txt',
        help = 'Read URLs from a text file.',
        nargs = '?'
    )
    parser.add_argument(
        '-f',
        '--final-path',
        default = 'Apple Music',
        help = 'Final Path.'
    )
    parser.add_argument(
        '-t',
        '--temp-path',
        default = 'temp',
        help = 'Temp Path.'
    )
    parser.add_argument(
        '-c',
        '--cookies-location',
        default = 'cookies.txt',
        help = 'Cookies location.'
    )
    parser.add_argument(
        '-d',
        '--disable-music-video-skip',
        action = 'store_true',
        help = 'Disable music video skip on playlists/albums.'
    )
    parser.add_argument(
        '-p',
        '--prefer-hevc',
        action = 'store_true',
        help = 'Prefer HEVC over AVC.'
    )
    parser.add_argument(
        '-n',
        '--no-lrc',
        action = 'store_true',
        help = "Don't create .lrc file."
    )
    parser.add_argument(
        '-s',
        '--skip-cleanup',
        action = 'store_true',
        help = 'Skip cleanup.'
    )
    parser.add_argument(
        '-e',
        '--print-exceptions',
        action = 'store_true',
        help = 'Print Execeptions.'
    )
    parser.add_argument(
        '-v',
        '--print-video-playlist',
        action = 'store_true',
        help = 'Print Video M3U8 Playlist.'
    )
    args = parser.parse_args()
    if not args.url and not args.urls_txt:
        parser.error('you must specify an url or a text file using -u/--urls-txt.')
    if args.urls_txt:
        with open(args.urls_txt, 'r', encoding = 'utf8') as f:
            args.url = f.read().splitlines()
    dl = Gamdl(
        args.disable_music_video_skip,
        args.cookies_location,
        args.temp_path,
        args.prefer_hevc,
        args.final_path,
        args.skip_cleanup,
        args.print_video_playlist,
        args.no_lrc
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
            print(f'* Failed to check URL {i + 1}.')
            if args.print_exceptions:
                traceback.print_exc()
    for i, url in enumerate(download_queue):
        for j, track in enumerate(url):
            print(f'Downloading "{track["title"]}" (track {j + 1} from URL {i + 1})...')
            track_id = track['track_id']
            try:
                webplayback = dl.get_webplayback(track_id)
                if 'alt_track_id' in track:
                    playlist = dl.get_playlist_music_video(webplayback)
                    stream_url_audio = dl.get_stream_url_music_video_audio(playlist)
                    decryption_keys_audio = dl.get_decryption_keys_music_video(stream_url_audio, track_id)
                    encrypted_location_audio = dl.get_encrypted_location('.m4a', track_id)
                    dl.download(encrypted_location_audio, stream_url_audio)
                    decrypted_location_audio = dl.get_decrypted_location('.m4a', track_id)
                    dl.decrypt(encrypted_location_audio, decrypted_location_audio, decryption_keys_audio)
                    stream_url_video = dl.get_stream_url_music_video_video(playlist)
                    decryption_keys_video = dl.get_decryption_keys_music_video(stream_url_video, track_id)
                    encrypted_location_video = dl.get_encrypted_location('.m4v', track_id)
                    dl.download(encrypted_location_video, stream_url_video)
                    decrypted_location_video = dl.get_decrypted_location('.m4v', track_id)
                    dl.decrypt(encrypted_location_video, decrypted_location_video, decryption_keys_video)
                    tags = dl.get_tags_music_video(track['alt_track_id'])
                    fixed_location = dl.get_fixed_location('.m4v', track_id)
                    final_location = dl.get_final_location('.m4v', tags)
                    dl.fixup_music_video(decrypted_location_audio, decrypted_location_video, fixed_location)
                    dl.make_final(final_location, fixed_location, tags)
                else:
                    stream_url = dl.get_stream_url_song(webplayback)
                    decryption_keys = dl.get_decryption_keys_song(stream_url, track_id)
                    encrypted_location = dl.get_encrypted_location('.m4a', track_id)
                    dl.download(encrypted_location, stream_url)
                    decrypted_location = dl.get_decrypted_location('.m4a', track_id)
                    dl.decrypt(encrypted_location, decrypted_location, decryption_keys)
                    lyrics = dl.get_lyrics(track_id)
                    tags = dl.get_tags_song(webplayback, lyrics)
                    fixed_location = dl.get_fixed_location('.m4a', track_id)
                    final_location = dl.get_final_location('.m4a', tags)
                    dl.fixup_song(decrypted_location, fixed_location)
                    dl.make_final(final_location, fixed_location, tags)
                    dl.make_lrc(final_location, lyrics)
            except KeyboardInterrupt:
                exit(1)
            except:
                error_count += 1
                print(f'* Failed to download "{track["title"]}" (track {j + 1} from URL {i + 1}).')
                if args.print_exceptions:
                    traceback.print_exc()
            dl.cleanup()
    print(f'Done ({error_count} error(s)).')
