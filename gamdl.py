from pathlib import Path
import re
import requests
import urllib3
import storefront_ids
import json
import m3u8
from yt_dlp import YoutubeDL
from pywidevine.L3.decrypt.wvdecrypt import WvDecrypt
from pywidevine.L3.decrypt.wvdecryptconfig import WvDecryptConfig
import base64
from pywidevine.L3.cdm.formats.widevine_pssh_data_pb2 import WidevinePsshData
from mutagen.mp4 import MP4Cover, MP4
import song_genres
import music_video_genres
from xml.dom import minidom
import datetime
import os
from argparse import ArgumentParser
import shutil
import traceback
import subprocess

class Gamdl:
    def __init__(self, disable_music_video_skip, auth_path, temp_path, prefer_hevc, final_path):
        self.disable_music_video_skip = disable_music_video_skip
        self.auth_path = auth_path
        self.temp_path = temp_path
        self.prefer_hevc = prefer_hevc
        self.final_path = final_path
        self.login()
    
    
    def login(self):
        cookies = {}
        with open(Path(self.auth_path) / 'cookies.txt', 'r') as f:
            for l in f:
                if not re.match(r"^#", l) and not re.match(r"^\n", l):
                    line_fields = l.strip().replace('&quot;', '"').split('\t')
                    cookies[line_fields[5]] = line_fields[6]
        with open(Path(self.auth_path) / 'token.txt', 'r') as f:
            token = f.read()
        self.session = requests.Session()
        self.session.verify = False
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        self.session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:95.0) Gecko/20100101 Firefox/95.0"})
        self.session.cookies.update(cookies)
        self.session.headers.update({
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.5",
            "Accept-Encoding": "gzip, deflate, br",
            "authorization": token,
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
                if 'playParams' in track['attributes'].keys():
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
            json.dumps({
                'salableAdamId': track_id
            })
        ).json()["songList"][0]
        return response
    

    def get_playlist_music_video(self, webplayback):
        return m3u8.load(webplayback['hls-playlist-url'])
    

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
        return Path(self.temp_path) / f'{track_id}_encrypted{extension}'
    

    def get_decrypted_location(self, extension, track_id):
        return Path(self.temp_path) / f'{track_id}_decrypted{extension}'
    

    def get_fixed_location(self, extension, track_id):
        return Path(self.temp_path) / f'{track_id}_fixed{extension}'
    

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
            json.dumps({
                'challenge': challenge,
                'key-system': 'com.widevine.alpha',
                'uri': track_uri,
                'adamId': track_id,
                'isLibrary': False,
                'user-initiated': True
            })
        ).json()['license']
        
    
    def decrypt_music_video(self, decrypted_location, encrypted_location, stream_url, track_id):
        playlist = m3u8.load(stream_url)
        track_uri = next(x for x in playlist.keys if x.keyformat == "urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed").uri
        wvdecryptconfig = WvDecryptConfig(decrypted_location, encrypted_location, track_uri)
        wvdecryptconfig.init_data_b64 = wvdecryptconfig.init_data_b64.split(",")[1]
        wvdecrypt = WvDecrypt(wvdecryptconfig)
        challenge = base64.b64encode(wvdecrypt.get_challenge()).decode('utf-8')
        license_b64 = self.get_license_b64(challenge, track_uri, track_id)
        wvdecrypt.update_license(license_b64)
        wvdecrypt.start_process()
    

    def decrypt_song(self, decrypted_location, encrypted_location, stream_url, track_id):
        track_uri = m3u8.load(stream_url).keys[0].uri
        wvpsshdata = WidevinePsshData()
        wvpsshdata.algorithm = 1
        wvdecryptconfig = WvDecryptConfig(decrypted_location, encrypted_location, track_uri)
        wvpsshdata.key_id.append(base64.b64decode(wvdecryptconfig.init_data_b64.split(",")[1]))
        wvdecryptconfig.init_data_b64 = base64.b64encode(wvpsshdata.SerializeToString()).decode("utf8")
        wvdecrypt = WvDecrypt(wvdecryptconfig)
        challenge = base64.b64encode(wvdecrypt.get_challenge()).decode('utf-8')
        license_b64 = self.get_license_b64(challenge, track_uri, track_id)
        wvdecrypt.update_license(license_b64)
        wvdecrypt.start_process()
    

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
        ms = int(unformatted_time.split('.')[-1])
        formated_time = datetime.datetime.fromtimestamp((s + m + ms)/1000.0)
        return f'{formated_time.minute:02d}:{formated_time.second:02d}.{int(str(formated_time.microsecond)[:2]):02d}'


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
        return [unsynced_lyrics.strip(), synced_lyrics]
    

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
            '\xa9alb': [metadata['playlistName']],
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
        if 'copyright' in metadata.keys():
            tags['cprt'] = [metadata['copyright']]
        if 'releaseDate' in metadata.keys():
            tags['\xa9day'] = [metadata['releaseDate']]
        if 'comments' in metadata.keys():
            tags['\xa9cmt'] = [metadata['comments']]
        if 'xid' in metadata.keys():
            tags['xid '] = [metadata['xid']]
        if 'composerId' in metadata.keys():
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
    

    def get_sanizated_string(self, dirty_string, is_folder = False):
        illegal_characters = ['\\', '/', ':', '*', '?', '"', '<', '>', '|', ';']
        for character in illegal_characters:
            dirty_string = dirty_string.replace(character, '_')
        if is_folder:
            dirty_string = dirty_string[:40]
            if dirty_string[-1:] == '.':
                dirty_string = dirty_string[:-1] + '_'
        else:
            dirty_string = dirty_string[:36]
        return dirty_string.strip()
    

    def get_final_location(self, file_extension, tags):
        final_location = Path(self.final_path)
        if 'plID' in tags.keys():
            if 'cpil' in tags.keys() and tags['cpil']:
                final_location /= f'Compilations/{self.get_sanizated_string(tags["©alb"][0], True)}'
            else:
                final_location /= f'{self.get_sanizated_string(tags["aART"][0], True)}/{self.get_sanizated_string(tags["©alb"][0], True)}'
            if tags['disk'][0][1] > 1:
                filename = self.get_sanizated_string(f'{tags["disk"][0][0]}-{tags["trkn"][0][0]:02d} {tags["©nam"][0]}')
            else:
                filename = self.get_sanizated_string(f'{tags["trkn"][0][0]:02d} {tags["©nam"][0]}')
        else:
            filename = self.get_sanizated_string(tags["©nam"][0])
            final_location /= f'{self.get_sanizated_string(tags["©ART"][0], True)}/Unknown Album/'
        final_location /= f'{filename}{file_extension}'
        return final_location
    

    def fixup_music_video(self, decrypted_location_audio, decrypted_location_video, fixed_location, final_location):
        os.makedirs(final_location.parents[0], exist_ok = True)
        subprocess.check_output(['MP4Box', '-quiet', '-add', decrypted_location_audio, '-add', decrypted_location_video, '-itags', 'title=placeholder', '-new', fixed_location])
        shutil.copy(fixed_location, final_location)
    

    def fixup_song(self, decrypted_location, fixed_location, final_location):
        os.makedirs(final_location.parents[0], exist_ok = True)
        subprocess.check_output(['MP4Box', '-quiet', '-add', decrypted_location, '-itags', 'title=placeholder', '-new', fixed_location])
        shutil.copy(fixed_location, final_location)
    

    def make_lrc(self, final_location, lyrics):
        with open(final_location.with_suffix('.lrc'), 'w', encoding = 'utf8') as f:
            f.write(lyrics[1])
    

    def apply_tags(self, final_location, tags):
        file = MP4(final_location).tags
        for key, value in tags.items():
            file[key] = value
        file.save(final_location)
    

if __name__ == '__main__':
    if not shutil.which('mp4decrypt'):
        print('mp4decrypt is not on PATH.')
        exit(1)
    if not shutil.which('MP4Box'):
        print('MP4Box is not on PATH.')
        exit(1)
    parser = ArgumentParser(description = 'A Python script to download Apple Music albums/music videos/playlists/songs.')
    parser.add_argument(
        'url',
        help='Apple Music albums/music videos/playlists/songs URL',
        nargs='*',
        metavar='<url>'
    )
    parser.add_argument(
        '-d',
        '--final-path',
        default = 'Apple Music',
        help = 'Set Final Path.',
        metavar = '<final path>'
    )
    parser.add_argument(
        '-a',
        '--auth-path',
        default = 'login',
        help = 'Set Auth Path.',
        metavar = '<auth path>'
    )
    parser.add_argument(
        '-m',
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
        '-t',
        '--temp-path',
        default = 'temp',
        help = 'Set Temp Path.',
        metavar = '<temp path>'
    )
    parser.add_argument(
        '-s',
        '--skip-cleanup',
        action = 'store_true',
        help = 'Skip cleanup.'
    )
    parser.add_argument(
        '-e',
        '--print-exception',
        action = 'store_true',
        help = 'Print Execeptions.'
    )
    parser.add_argument(
        '-v',
        '--print-video-playlist',
        action = 'store_true',
        help = 'Print Video Playlist.'
    )
    parser.add_argument(
        '-u',
        '--urls-txt',
        action = 'store_true',
        help = 'Use urls.txt to download URLs.'
    )
    args = parser.parse_args()
    if args.urls_txt:
        with open('urls.txt', 'r', encoding = 'utf8') as f:
            args.url = f.read().splitlines()
    elif not args.url:
        parser.error('the following arguments are required: <url>')
    gamdl = Gamdl(args.disable_music_video_skip, args.auth_path, args.temp_path, args.prefer_hevc, args.final_path)
    error_count = 0
    download_queue = []
    for i in range(len(args.url)):
        try:
            download_queue.append(gamdl.get_download_queue(args.url[i]))
        except KeyboardInterrupt:
            exit(1)
        except:
            error_count += 1
            print(f'* Failed to check URL {i + 1}.')
            if args.print_exception:
                traceback.print_exc()
        if not download_queue:
            print('* Failed to check all URLs.')
            exit(1)
    for i in range(len(download_queue)):
        for j in range(len(download_queue[i])):
            print(f'Downloading "{download_queue[i][j]["title"]}" (track {j + 1} from URL {i + 1})...')
            track_id = download_queue[i][j]['track_id']
            try:
                webplayback = gamdl.get_webplayback(track_id)
                if 'alt_track_id' in download_queue[i][j]:
                    playlist = gamdl.get_playlist_music_video(webplayback)
                    if args.print_video_playlist:
                        print(playlist.dumps())
                    stream_url_audio = gamdl.get_stream_url_music_video_audio(playlist)
                    encrypted_location_audio = gamdl.get_encrypted_location('.m4a', track_id)
                    gamdl.download(encrypted_location_audio, stream_url_audio)
                    decrypted_location_audio = gamdl.get_decrypted_location('.m4a', track_id)
                    gamdl.decrypt_music_video(decrypted_location_audio, encrypted_location_audio, stream_url_audio, track_id)
                    stream_url_video = gamdl.get_stream_url_music_video_video(playlist)
                    encrypted_location_video = gamdl.get_encrypted_location('.m4v', track_id)
                    gamdl.download(encrypted_location_video, stream_url_video)
                    decrypted_location_video = gamdl.get_decrypted_location('.m4v', track_id)
                    gamdl.decrypt_music_video(decrypted_location_video, encrypted_location_video, stream_url_video, track_id)
                    tags = gamdl.get_tags_music_video(download_queue[i][j]['alt_track_id'])
                    fixed_location = gamdl.get_fixed_location('.m4v', track_id)
                    final_location = gamdl.get_final_location('.m4v', tags)
                    gamdl.fixup_music_video(decrypted_location_audio, decrypted_location_video, fixed_location, final_location)
                    gamdl.apply_tags(final_location, tags)
                else:
                    stream_url = gamdl.get_stream_url_song(webplayback)
                    encrypted_location = gamdl.get_encrypted_location('.m4a', track_id)
                    gamdl.download(encrypted_location, stream_url)
                    decrypted_location = gamdl.get_decrypted_location('.m4a', track_id)
                    gamdl.decrypt_song(decrypted_location, encrypted_location, stream_url, track_id)
                    lyrics = gamdl.get_lyrics(track_id)
                    tags = gamdl.get_tags_song(webplayback, lyrics)
                    fixed_location = gamdl.get_fixed_location('.m4a', track_id)
                    final_location = gamdl.get_final_location('.m4a', tags)
                    gamdl.fixup_song(decrypted_location, fixed_location, final_location)
                    if not args.no_lrc and lyrics and lyrics[1]:
                        gamdl.make_lrc(final_location, lyrics)
                    gamdl.apply_tags(final_location, tags)
            except KeyboardInterrupt:
                exit(1)
            except:
                error_count += 1
                print(f'* Failed to download "{download_queue[i][j]["title"]}" (track {j + 1} from URL {i + 1}).')
                if args.print_exception:
                    traceback.print_exc()
            if not args.skip_cleanup:
                shutil.rmtree(gamdl.temp_path)
    print(f'Finished ({error_count} error(s)).')


