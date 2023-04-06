from pathlib import Path
import glob
from http.cookiejar import MozillaCookieJar
import re
import base64
import datetime
from xml.etree import ElementTree
import functools
import subprocess
import shutil
import gamdl.storefront_ids
from pywidevine import Cdm, Device, PSSH, WidevinePsshData
import requests
import m3u8
from yt_dlp import YoutubeDL
from mutagen.mp4 import MP4, MP4Cover


class Gamdl:
    def __init__(self, wvd_location, cookies_location, disable_music_video_skip, prefer_hevc, heaac, temp_path, final_path, no_lrc, overwrite, skip_cleanup):
        self.disable_music_video_skip = disable_music_video_skip
        self.prefer_hevc = prefer_hevc
        if heaac:
            self.song_audio_quality = '32:ctrp64'
            self.music_video_audio_quality = 'audio-HE-stereo-64'
        else:
            self.song_audio_quality = '28:ctrp256'
            self.music_video_audio_quality = 'audio-stereo-256'
        self.temp_path = Path(temp_path)
        self.final_path = Path(final_path)
        self.no_lrc = no_lrc
        self.overwrite = overwrite
        self.skip_cleanup = skip_cleanup
        wvd_location = glob.glob(wvd_location)
        if not wvd_location:
            raise Exception('.wvd file not found')
        self.cdm = Cdm.from_device(Device.load(Path(wvd_location[0])))
        self.cdm_session = self.cdm.open()
        cookies = MozillaCookieJar(Path(cookies_location))
        cookies.load(ignore_discard=True, ignore_expires=True)
        self.session = requests.Session()
        self.session.cookies.update(cookies)
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:95.0) Gecko/20100101 Firefox/95.0',
            'Accept': 'application/json',
            'Accept-Language': 'en-US,en;q=0.5',
            'Accept-Encoding': 'gzip, deflate, br',
            'content-type': 'application/json',
            'Media-User-Token': self.session.cookies.get_dict()['media-user-token'],
            'x-apple-renewal': 'true',
            'DNT': '1',
            'Connection': 'keep-alive',
            'Sec-Fetch-Dest': 'empty',
            'Sec-Fetch-Mode': 'cors',
            'Sec-Fetch-Site': 'same-site',
            'origin': 'https://beta.music.apple.com',
        })
        web_page = self.session.get('https://beta.music.apple.com').text
        index_js_uri = re.search('(?<=index\.)(.*?)(?=\.js")', web_page).group(1)
        index_js_page = self.session.get(f'https://beta.music.apple.com/assets/index.{index_js_uri}.js').text
        token = re.search('(?=eyJh)(.*?)(?=")', index_js_page).group(1)
        self.session.headers.update({"authorization": f'Bearer {token}'})
        self.country = self.session.cookies.get_dict()['itua']
        self.storefront = getattr(gamdl.storefront_ids, self.country.upper())
    

    def get_download_queue(self, url):
        download_queue = []
        product_id = url.split('/')[-1].split('i=')[-1].split('&')[0].split('?')[0]
        response = self.session.get(f'https://amp-api.music.apple.com/v1/catalog/{self.country}/?ids[songs]={product_id}&ids[albums]={product_id}&ids[playlists]={product_id}&ids[music-videos]={product_id}').json()['data'][0]
        if response['type'] in ('songs', 'music-videos') and 'playParams' in response['attributes']:
            download_queue.append(response)
        if response['type'] == 'albums' or response['type'] == 'playlists':
            for track in response['relationships']['tracks']['data']:
                if 'playParams' in track['attributes']:
                    if track['type'] == 'music-videos' and self.disable_music_video_skip:
                        download_queue.append(track)
                    if track['type'] == 'songs':
                        download_queue.append(track)
        if not download_queue:
            raise Exception('Criteria not met')
        return download_queue


    def get_webplayback(self, track_id):
        response = self.session.post(
            'https://play.itunes.apple.com/WebObjects/MZPlay.woa/wa/webPlayback',
            json = {
                'salableAdamId': track_id,
                'language': 'en-US',
            }
        ).json()["songList"][0]
        return response
    

    def get_stream_url_song(self, webplayback):
        return next(i for i in webplayback["assets"] if i["flavor"] == self.song_audio_quality)['URL']
    

    def get_stream_url_music_video(self, webplayback):
        with YoutubeDL({
            'allow_unplayable_formats': True,
            'quiet': True,
            'no_warnings': True,
        }) as ydl:
            playlist = ydl.extract_info(webplayback['hls-playlist-url'], download = False)
        if self.prefer_hevc:
            stream_url_video = playlist['formats'][-1]['url']
        else:
            stream_url_video = [i['url'] for i in playlist['formats'] if i['vcodec'] is not None and 'avc1' in i['vcodec']][-1]
        stream_url_audio = next(i['url'] for i in playlist['formats'] if self.music_video_audio_quality in i['format_id'])
        return stream_url_video, stream_url_audio
    

    def check_exists(self, final_location):
        return Path(final_location).exists()
    

    def get_encrypted_location_video(self, track_id):
        return self.temp_path / f'{track_id}_encrypted_video.mp4'
    

    def get_encrypted_location_audio(self, track_id):
        return self.temp_path / f'{track_id}_encrypted_audio.mp4'
    

    def get_decrypted_location_video(self, track_id):
        return self.temp_path / f'{track_id}_decrypted_video.mp4'


    def get_decrypted_location_audio(self, track_id):
        return self.temp_path / f'{track_id}_decrypted_audio.mp4'
    

    def get_fixed_location(self, track_id, file_extension):
        return self.temp_path / f'{track_id}_fixed{file_extension}'
    

    def download(self, encrypted_location, stream_url):
        with YoutubeDL({
            'quiet': True,
            'no_warnings': True,
            'outtmpl': str(encrypted_location),
            'allow_unplayable_formats': True,
            'fixup': 'never',
            'overwrites': self.overwrite,
            'external_downloader': 'aria2c',
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
                'user-initiated': True,
            }
        ).json()['license']


    def get_decryption_keys_music_video(self, stream_url, track_id):
        playlist = m3u8.load(stream_url)
        track_uri = next(i for i in playlist.keys if i.keyformat == "urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed").uri
        pssh = PSSH(track_uri.split(',')[1])
        challenge = base64.b64encode(self.cdm.get_license_challenge(self.cdm_session, pssh)).decode('utf-8')
        license_b64 = self.get_license_b64(challenge, track_uri, track_id)
        self.cdm.parse_license(self.cdm_session, license_b64)
        return f'1:{next(i for i in self.cdm.get_keys(self.cdm_session) if i.type == "CONTENT").key.hex()}'

    
    def get_decryption_keys_song(self, stream_url, track_id):
        track_uri = m3u8.load(stream_url).keys[0].uri
        widevine_pssh_data = WidevinePsshData()
        widevine_pssh_data.algorithm = 1
        widevine_pssh_data.key_ids.append(base64.b64decode(track_uri.split(",")[1]))
        pssh = PSSH(base64.b64encode(widevine_pssh_data.SerializeToString()).decode('utf-8'))
        challenge = base64.b64encode(self.cdm.get_license_challenge(self.cdm_session, pssh)).decode('utf-8')
        license_b64 = self.get_license_b64(challenge, track_uri, track_id)
        self.cdm.parse_license(self.cdm_session, license_b64)
        return f'1:{next(i for i in self.cdm.get_keys(self.cdm_session) if i.type == "CONTENT").key.hex()}'


    def decrypt(self, encrypted_location, decrypted_location, decryption_keys):
        subprocess.run(
            [
                'mp4decrypt',
                encrypted_location,
                '--key',
                decryption_keys,
                decrypted_location,
            ],
            check=True
        )

    
    def get_synced_lyrics_formated_time(self, unformatted_time):
        unformatted_time = unformatted_time.replace('m', '').replace('s', '').replace(':', '.')
        unformatted_time = unformatted_time.split('.')
        m, s, ms = 0, 0, 0
        ms = int(unformatted_time[-1])
        if len(unformatted_time) >= 2:
            s = int(unformatted_time[-2]) * 1000
        if len(unformatted_time) >= 3:
            m = int(unformatted_time[-3]) * 60000
        unformatted_time = datetime.datetime.fromtimestamp((ms + s + m)/1000.0)
        ms_new = f'{int(str(unformatted_time.microsecond)[:3]):03d}'
        if int(ms_new[2]) >= 5:
            ms = int(f'{int(ms_new[:2]) + 1}') * 10
            unformatted_time += datetime.timedelta(milliseconds=ms) - datetime.timedelta(microseconds=unformatted_time.microsecond)
        return unformatted_time.strftime('%M:%S.%f')[:-4]


    def get_lyrics(self, track_id):
        try:
            lyrics_ttml = ElementTree.fromstring(self.session.get(f'https://amp-api.music.apple.com/v1/catalog/{self.country}/songs/{track_id}/lyrics').json()['data'][0]['attributes']['ttml'])
        except:
            return None, None
        unsynced_lyrics = ''
        synced_lyrics = ''
        for div in lyrics_ttml.iter('{http://www.w3.org/ns/ttml}div'):
            for p in div.iter('{http://www.w3.org/ns/ttml}p'):
                if p.attrib.get('begin'):
                    synced_lyrics += f'[{self.get_synced_lyrics_formated_time(p.attrib.get("begin"))}]{p.text}\n'
                if p.text is not None:
                    unsynced_lyrics += p.text + '\n'
            unsynced_lyrics += '\n'
        return unsynced_lyrics[:-2], synced_lyrics
    

    @functools.lru_cache()
    def get_cover(self, url):
        return requests.get(url).content
    

    def get_tags_song(self, webplayback, unsynced_lyrics):
        metadata = next(i for i in webplayback["assets"] if i["flavor"] == self.song_audio_quality)['metadata']
        cover_url = next(i for i in webplayback["assets"] if i["flavor"] == self.song_audio_quality)['artworkURL']
        tags = {
            '\xa9nam': [metadata['itemName']],
            '\xa9gen': [metadata['genre']],
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
            'covr': [MP4Cover(self.get_cover(cover_url), MP4Cover.FORMAT_JPEG)],
            'stik': [1],
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
        if unsynced_lyrics:
            tags['\xa9lyr'] = [unsynced_lyrics]
        return tags


    def get_tags_music_video(self, track_id):
        metadata = requests.get(f'https://itunes.apple.com/lookup?id={track_id}&entity=album&country={self.country}&lang=en_US').json()['results']
        extra_metadata = requests.get(f'https://music.apple.com/music-video/{metadata[0]["trackId"]}', headers = {'X-Apple-Store-Front': f'{self.storefront} t:music31'}).json()['storePlatformData']['product-dv']['results'][str(metadata[0]['trackId'])]
        tags = {
            '\xa9ART': [metadata[0]["artistName"]],
            '\xa9nam': [metadata[0]["trackCensoredName"]],
            '\xa9day': [metadata[0]["releaseDate"]],
            '\xa9gen': [metadata[0]['primaryGenreName']],
            'stik': [6],
            'atID': [metadata[0]['artistId']],
            'cnID': [metadata[0]["trackId"]],
            'geID': [int(extra_metadata['genres'][0]['genreId'])],
            'sfID': [int(self.storefront.split('-')[0])],
            'covr': [MP4Cover(self.get_cover(metadata[0]["artworkUrl30"].replace('30x30bb.jpg', '600x600bb.jpg')), MP4Cover.FORMAT_JPEG)],
        }
        if 'copyright' in extra_metadata:
            tags['cprt'] = [extra_metadata['copyright']]
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
            if file_extension == '.m4v' and final_location.exists() and MP4(final_location)['cnID'][0] != tags['cnID'][0]:
                final_location = self.get_final_location_overwrite_prevented_music_video(final_location)
        except:
            pass
        return final_location
    

    def fixup_music_video(self, decrypted_location_audio, decrypted_location_video, fixed_location):
        subprocess.run(
            [
                'MP4Box',
                '-quiet',
                '-add',
                decrypted_location_audio,
                '-add',
                decrypted_location_video,
                '-itags',
                'artist=placeholder',
                '-new',
                fixed_location,
            ],
            check=True
        )
    

    def fixup_song(self, decrypted_location, fixed_location):
        subprocess.run(
            [
                'MP4Box',
                '-quiet',
                '-add',
                decrypted_location,
                '-itags',
                'artist=placeholder',
                '-new',
                fixed_location,
            ],
            check=True
        )
    

    def make_lrc(self, final_location, synced_lyrics):
        if synced_lyrics and not self.no_lrc:
            with open(final_location.with_suffix('.lrc'), 'w', encoding = 'utf8') as f:
                f.write(synced_lyrics)
    

    def make_final(self, final_location, fixed_location, tags):
        final_location.parent.mkdir(parents = True, exist_ok = True)
        shutil.copy(fixed_location, final_location)
        file = MP4(final_location)
        file.update(tags)
        file.save()
    

    def cleanup(self):
        if self.temp_path.exists() and not self.skip_cleanup:
            shutil.rmtree(self.temp_path)
