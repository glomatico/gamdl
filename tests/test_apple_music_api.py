import asyncio
import tempfile
import unittest
from pathlib import Path

from gamdl.api.apple_music_api import _matches_apple_music_cookie_domain
from gamdl.api.apple_music_api import AppleMusicApi


class AppleMusicApiCookieTests(unittest.TestCase):
    def test_accepts_music_domain_cookie(self):
        self.assertTrue(_matches_apple_music_cookie_domain(".music.apple.com"))

    def test_accepts_apple_domain_cookie(self):
        self.assertTrue(_matches_apple_music_cookie_domain(".apple.com"))

    def test_rejects_non_apple_domain_cookie(self):
        self.assertFalse(_matches_apple_music_cookie_domain(".example.com"))

    def test_create_from_netscape_cookies_accepts_apple_domain(self):
        cookie_file = tempfile.NamedTemporaryFile("w", delete=False)
        try:
            cookie_file.write("# Netscape HTTP Cookie File\n")
            cookie_file.write(
                ".apple.com\tTRUE\t/\tTRUE\t2147483647\tmedia-user-token\ttest-token\n"
            )
            cookie_file.close()

            async def run():
                original_create = AppleMusicApi.create

                async def fake_create(*args, **kwargs):
                    return kwargs

                AppleMusicApi.create = fake_create
                try:
                    return await AppleMusicApi.create_from_netscape_cookies(
                        cookie_file.name
                    )
                finally:
                    AppleMusicApi.create = original_create

            result = asyncio.run(run())
            self.assertEqual(result["media_user_token"], "test-token")
        finally:
            Path(cookie_file.name).unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
