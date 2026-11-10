"""Public and private transport boundaries, redirect safety and compatibility."""

import unittest

import httpx

from gamdl.api import ApiHttpClient as LegacyApiHttpClient
from gamdl.api import RequestPacer as LegacyRequestPacer
from gamdl.api.http import ApiHttpClient as LegacyHttpModuleClient
from gamdl.transport import (
    ApiHttpClient,
    PublicTransport,
    PublicTransportError,
    RequestPacer,
)


class TransportTests(unittest.IsolatedAsyncioTestCase):
    def test_private_exports_preserve_existing_class_identity(self):
        self.assertIs(ApiHttpClient, LegacyApiHttpClient)
        self.assertIs(ApiHttpClient, LegacyHttpModuleClient)
        self.assertIs(RequestPacer, LegacyRequestPacer)

    async def test_public_redirects_use_no_client_headers_cookies_or_auth(self):
        observed = []

        def handler(request):
            observed.append(request)
            if request.url.path == "/start":
                return httpx.Response(
                    302, headers={"Location": "https://other.example.test/end"}
                )
            return httpx.Response(206, content=b"public bytes")

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            headers={
                "Authorization": "Bearer private",
                "Cookie": "session=private",
            },
            auth=httpx.BasicAuth("username", "secret"),
        ) as client:
            client.cookies.set(
                "session", "redirect-cookie", domain="other.example.test"
            )
            public = PublicTransport(client)
            self.assertEqual(
                await public.fetch("https://cdn.example.test/start", byte_range="5-9"),
                b"public bytes",
            )
            self.assertFalse(client.is_closed)

        self.assertEqual(len(observed), 2)
        for request in observed:
            self.assertNotIn("authorization", request.headers)
            self.assertNotIn("cookie", request.headers)
            self.assertEqual(request.headers["range"], "bytes=5-9")

    async def test_public_rejects_insecure_or_credential_bearing_redirects(self):
        for url in (
            "http://cdn.example.test/insecure",
            "https://user:password@cdn.example.test/private",
            "file:///etc/passwd",
        ):
            with self.subTest(url=url):
                seen = []

                def handler(request, *, observations=seen, destination=url):
                    observations.append(request)
                    return httpx.Response(302, headers={"Location": destination})

                async with httpx.AsyncClient(
                    transport=httpx.MockTransport(handler)
                ) as client:
                    with self.assertRaises(PublicTransportError) as captured:
                        await PublicTransport(client).fetch(
                            "https://cdn.example.test/start"
                        )
                self.assertEqual(captured.exception.reason, "invalid_url")
                self.assertEqual(len(seen), 1)

    async def test_public_redirect_limits_and_missing_location(self):
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(302, headers={"Location": "/repeat"})
            )
        ) as client:
            with self.assertRaises(PublicTransportError) as captured:
                await PublicTransport(client).fetch("https://cdn.example.test/repeat")
            self.assertEqual(captured.exception.reason, "too_many_redirects")

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _request: httpx.Response(302))
        ) as client:
            with self.assertRaises(PublicTransportError) as captured:
                await PublicTransport(client).fetch("https://cdn.example.test/start")
            self.assertEqual(captured.exception.reason, "missing_redirect")

    async def test_public_missing_artwork_and_http_errors_are_distinguished(self):
        def handler(request):
            return httpx.Response(404 if request.url.path == "/missing" else 503)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            public = PublicTransport(client)
            self.assertIsNone(
                await public.fetch(
                    "https://cdn.example.test/missing", allow_not_found=True
                )
            )
            for url in (
                "https://cdn.example.test/missing?token=private",
                "https://cdn.example.test/unavailable?token=private",
            ):
                with self.subTest(url=url):
                    with self.assertRaises(PublicTransportError) as captured:
                        await public.fetch(url)
                    self.assertEqual(captured.exception.reason, "request_failed")
                    self.assertNotIn("private", str(captured.exception))


if __name__ == "__main__":
    unittest.main()
