"""Authentication, credential/device prompts and session startup UI."""

from contextlib import AsyncExitStack

from rich.console import Console

from gamdl.api import AppleMusicApi, WrapperApi
from gamdl.drm import Drm

from .prompts import ask_file, ask_text


async def authenticate(options, stack: AsyncExitStack, *, console: Console):
    """Initialize an Apple Music API; pause the spinner for Questionary prompts."""
    if options.auth_mode == "cookies":
        cookies = await ask_file(options.cookies_path, label="Apple Music cookies")
        with console.status("Authenticating...", spinner="dots"):
            api = await stack.enter_async_context(
                await AppleMusicApi.from_cookies(
                    str(cookies),
                    storefront=options.storefront,
                    language=options.language,
                )
            )
        return api, None

    # The wrapper requests credentials only if its session is logged out.
    # The spinner must not draw over Questionary when that happens.
    status = console.status("Authenticating...", spinner="dots")

    async def credentials():
        status.stop()
        try:
            return (
                await ask_text("Apple ID:"),
                await ask_text("Password:", password=True),
            )
        finally:
            status.start()

    async def two_factor():
        status.stop()
        try:
            return await ask_text("Two-factor authentication code:")
        finally:
            status.start()

    with status:
        wrapper = await stack.enter_async_context(
            await WrapperApi.create(
                base_url=options.wrapper_url,
                decrypt_host=options.wrapper_decrypt_host,
                decrypt_port=options.wrapper_decrypt_port,
                get_credentials=credentials,
                get_2fa_code=two_factor,
            )
        )
        api = await stack.enter_async_context(
            await AppleMusicApi.from_wrapper(
                wrapper,
                storefront=options.storefront,
                language=options.language,
            )
        )
    return api, wrapper


async def create_drm(options, api):
    device_path = options.device_path
    if options.drm_backend == "playready":
        device_path = str(
            await ask_file(device_path, label="PlayReady .prd", extension=".prd")
        )
        return Drm.from_playready(api, device_path=device_path)
    if device_path:
        device_path = str(await ask_file(device_path, label="Widevine device"))
    return Drm.from_widevine(api, device_path=device_path)
