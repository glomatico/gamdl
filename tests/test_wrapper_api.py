import httpx

from gamdl.api.wrapper import WrapperApi


def test_wrapper_api_is_http_control_plane_only():
    api = WrapperApi(
        "http://127.0.0.1",
        "127.0.0.1",
        10020,
        httpx.AsyncClient(),
        {"auth": {"state": "authenticated"}},
    )
    try:
        assert api.base_url == "http://127.0.0.1"
        assert api.decrypt_host == "127.0.0.1"
        assert api.decrypt_port == 10020
        assert not hasattr(api, "decrypt")
    finally:
        import anyio

        anyio.run(api.client.aclose)
