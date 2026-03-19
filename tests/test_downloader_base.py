import unittest

from gamdl.downloader.downloader_base import AppleMusicBaseDownloader


class AppleMusicBaseDownloaderTests(unittest.TestCase):
    def test_wrapper_m3u8_ip_uses_matching_port_offset(self):
        downloader = AppleMusicBaseDownloader.__new__(AppleMusicBaseDownloader)
        downloader.wrapper_decrypt_ip = "127.0.0.1:10020"
        self.assertEqual(downloader.get_wrapper_m3u8_ip(), "127.0.0.1:20020")

    def test_wrapper_m3u8_ip_tracks_custom_decrypt_port(self):
        downloader = AppleMusicBaseDownloader.__new__(AppleMusicBaseDownloader)
        downloader.wrapper_decrypt_ip = "127.0.0.1:10022"
        self.assertEqual(downloader.get_wrapper_m3u8_ip(), "127.0.0.1:20022")
