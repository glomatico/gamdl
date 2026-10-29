"""Authentication, credential/device prompts and session startup UI."""

from contextlib import AsyncExitStack

import click
from rich.console import Console

from gamdl.api import AppleMusicApi, WrapperApi
from gamdl.drm import Drm

from .config.loader import ConfigError, save_auth_configuration
from .prompts import ask_file, ask_text, require_terminal
from .tui.components import MenuChoice
from .tui.selector import select_one


async def authenticate(
    options, stack: AsyncExitStack, *, console: Console, force_setup: bool = False
):
    """Authenticate, offering setup when no method was configured."""
    setup = force_setup or options.auth_mode is None
    if setup:
        require_terminal("Authentication setup")
        console.print(
            "[bold cyan]Configure Gamdl authentication[/bold cyan]"
            if force_setup
            else "[bold cyan]Welcome to Gamdl![/bold cyan]"
        )
        console.print("Choose how to authenticate with Apple Music.\n")
        methods = ("wrapper", "cookies", "media-user-token")
        options.auth_mode = methods[
            await select_one(
                "Authentication method",
                [
                    MenuChoice("Wrapper", 0, "Connect to a wrapper service"),
                    MenuChoice("Cookies", 1, "Use an exported cookies file"),
                    MenuChoice("Media User Token", 2, "Enter the token directly"),
                ],
            )
        ]
        if options.auth_mode == "wrapper":
            url = await ask_text("Wrapper URL:", default=options.wrapper_url)
            options.wrapper_url = url.strip()
            if not options.wrapper_url:
                raise click.ClickException("Wrapper URL cannot be empty")
        elif options.auth_mode == "cookies":
            path = await ask_text("Cookies file:", default=options.cookies_path or "")
            options.cookies_path = str(
                await ask_file(path.strip() or None, label="Apple Music cookies")
            )
        else:
            token = await ask_text("Media User Token:", password=True)
            options.media_user_token = token.strip()
            if not options.media_user_token:
                raise click.ClickException("Media User Token cannot be empty")

    api, wrapper = await _authenticate_selected(options, stack, console=console)
    if setup:
        ctx = click.get_current_context(silent=True)
        root = ctx.find_root().obj if ctx is not None else None
        if root is not None and not root["no_config"]:
            try:
                save_auth_configuration(
                    root["config_path"],
                    mode=options.auth_mode,
                    cookies_path=options.cookies_path,
                    wrapper_url=options.wrapper_url,
                    media_user_token=options.media_user_token,
                    profile=root["profile"],
                )
            except ConfigError as exc:
                raise click.ClickException(str(exc)) from None
            console.print("[green]✓[/green] Authentication settings saved")
        else:
            console.print(
                "[yellow]![/yellow] Authentication settings not saved "
                "(--no-config or no Click context)"
            )
    return api, wrapper


async def _authenticate_selected(options, stack: AsyncExitStack, *, console: Console):
    """Use the selected method, keeping credentials out of console output."""
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

    if options.auth_mode == "media-user-token":
        token = options.media_user_token
        if not token:
            token = (await ask_text("Media User Token:", password=True)).strip()
        if not token:
            raise click.ClickException("A Media User Token is required")
        with console.status("Authenticating...", spinner="dots"):
            api = await stack.enter_async_context(
                await AppleMusicApi.create(
                    media_user_token=token,
                    storefront=options.storefront,
                    language=options.language,
                )
            )
        return api, None

    if options.auth_mode != "wrapper":
        raise click.ClickException(
            "Authentication method is not configured (run 'gamdl auth')"
        )

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
