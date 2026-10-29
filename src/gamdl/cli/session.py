"""Authentication, credential/device prompts and session startup UI."""

from contextlib import AsyncExitStack, nullcontext
from pathlib import Path

import click
from rich.console import Console

from gamdl.api import (
    AppleMusicApi,
    GamdlApiError,
    GamdlAuthenticationError,
    GamdlRateLimitError,
    GamdlTransportError,
    WrapperApi,
)
from gamdl.drm import Drm

from .config.loader import ConfigError, save_auth_configuration
from .prompts import ask_file, ask_text, require_terminal
from .tui.components import MenuChoice
from .tui.wizard import FullScreenWizard, WizardBack


async def authenticate(
    options, stack: AsyncExitStack, *, console: Console, force_setup: bool = False
):
    """Authenticate, offering setup when no method was configured."""
    if force_setup or options.auth_mode is None:
        require_terminal("Authentication setup")
        return await _authentication_wizard(options, stack, force_setup=force_setup)
    return await _authenticate_selected(options, stack, console=console)


def _auth_error(exc: Exception) -> str:
    """Present actionable errors without exposing tokens, passwords or URLs."""
    if isinstance(exc, WizardBack):
        return "Authentication cancelled"
    if isinstance(exc, GamdlRateLimitError):
        return "Apple Music rate limit reached. Try again later."
    if isinstance(exc, GamdlAuthenticationError):
        return "Credentials were rejected. Check the login information."
    if isinstance(exc, GamdlTransportError):
        return "Unable to connect. Check the wrapper address and your connection."
    if isinstance(exc, GamdlApiError):
        return "Apple Music or the wrapper returned an error."
    if isinstance(exc, ConfigError):
        return "Unable to save authentication settings. Check your config file."
    return "Authentication failed. Check your settings and try again."


async def _authentication_wizard(options, stack: AsyncExitStack, *, force_setup: bool):
    """Keep method, credentials, network calls and errors on one alt-screen."""
    wizard = FullScreenWizard(summary="Apple Music authentication")
    methods = ("wrapper", "cookies", "media-user-token")

    async def flow(ui: FullScreenWizard):
        while True:
            choice = await ui.choose(
                "Configure authentication" if force_setup else "Welcome to Gamdl!",
                [
                    MenuChoice("Wrapper", 0, "Connect to a wrapper service"),
                    MenuChoice("Cookies", 1, "Use an exported cookies file"),
                    MenuChoice("Media User Token", 2, "Enter your token directly"),
                ],
                description="Choose how to authenticate with Apple Music.",
            )
            options.auth_mode = methods[choice]
            try:
                while True:
                    if options.auth_mode == "wrapper":
                        value = await ui.ask(
                            "Wrapper authentication",
                            "Wrapper URL",
                            description="Address of your wrapper service",
                            default=options.wrapper_url,
                        )
                        if value.strip():
                            options.wrapper_url = value.strip()
                            break
                        await ui.choose(
                            "Wrapper URL required",
                            [MenuChoice("Enter wrapper URL", 0)],
                            description="The wrapper address cannot be empty.",
                        )
                    elif options.auth_mode == "cookies":
                        value = await ui.ask(
                            "Cookies authentication",
                            "Cookies file",
                            description="Path to the exported Apple Music cookies.txt",
                            default=options.cookies_path or "",
                        )
                        path = Path(value.strip().strip("\"'")).expanduser()
                        if value.strip() and path.is_file():
                            options.cookies_path = str(path)
                            break
                        await ui.choose(
                            "Invalid cookies file",
                            [MenuChoice("Try another path", 0)],
                            description="The file does not exist or is not readable.",
                        )
                    else:
                        value = await ui.ask(
                            "Media User Token",
                            "Media User Token",
                            description="Paste your Apple Music media-user-token",
                            password=True,
                        )
                        if value.strip():
                            options.media_user_token = value.strip()
                            break
                        await ui.choose(
                            "Token required",
                            [MenuChoice("Enter token", 0)],
                            description="Media User Token cannot be empty.",
                        )
            except WizardBack:
                continue

            while True:
                ui.status("Authenticating", "Connecting to Apple Music...")
                attempt = AsyncExitStack()
                promoted = False

                async def ask_wrapper(label: str, *, password: bool = False) -> str:
                    return await ui.ask(
                        "Wrapper sign in",
                        label,
                        description="Sign in through your wrapper service",
                        password=password,
                    )

                try:
                    api, wrapper = await _authenticate_selected(
                        options,
                        attempt,
                        console=Console(quiet=True),
                        ask_value=ask_wrapper,
                        show_status=False,
                    )
                    ctx = click.get_current_context(silent=True)
                    root = ctx.find_root().obj if ctx is not None else None
                    if root is not None and not root["no_config"]:
                        save_auth_configuration(
                            root["config_path"],
                            mode=options.auth_mode,
                            cookies_path=options.cookies_path,
                            wrapper_url=options.wrapper_url,
                            media_user_token=options.media_user_token,
                            profile=root["profile"],
                        )
                    stack.push_async_callback(attempt.aclose)
                    promoted = True
                    ui.status("Authenticated", "Your Apple Music session is ready.")
                    try:
                        await ui.choose(
                            "Authentication successful",
                            [MenuChoice("Continue", 0)],
                            description=(
                                "Settings saved. Press Enter to continue."
                                if root is not None and not root["no_config"]
                                else "Session ready. --no-config: settings were not saved."
                            ),
                        )
                    except WizardBack:
                        pass
                    return api, wrapper
                except WizardBack:
                    break
                except Exception as exc:  # noqa: BLE001 - UI must recover from failed auth
                    try:
                        action = await ui.choose(
                            "Authentication failed",
                            [
                                MenuChoice("Retry", 0),
                                MenuChoice("Change authentication method", 1),
                            ],
                            description=_auth_error(exc),
                        )
                    except WizardBack:
                        break
                    if action == 1:
                        break
                finally:
                    if not promoted:
                        await attempt.aclose()

    return await wizard.run(flow)


async def _authenticate_selected(
    options,
    stack: AsyncExitStack,
    *,
    console: Console,
    ask_value=None,
    show_status: bool = True,
):
    """Use the selected method, keeping credentials out of console output."""
    if options.auth_mode == "cookies":
        cookies = await ask_file(options.cookies_path, label="Apple Music cookies")
        with (
            console.status("Authenticating...", spinner="dots")
            if show_status
            else nullcontext()
        ):
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
        with (
            console.status("Authenticating...", spinner="dots")
            if show_status
            else nullcontext()
        ):
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
    status = (
        console.status("Authenticating...", spinner="dots")
        if show_status
        else nullcontext()
    )

    async def credentials():
        if show_status:
            status.stop()
        try:
            return (
                await (ask_value("Apple ID") if ask_value else ask_text("Apple ID:")),
                await (
                    ask_value("Password", password=True)
                    if ask_value
                    else ask_text("Password:", password=True)
                ),
            )
        finally:
            if show_status:
                status.start()

    async def two_factor():
        if show_status:
            status.stop()
        try:
            return await (
                ask_value("Two-factor authentication code")
                if ask_value
                else ask_text("Two-factor authentication code:")
            )
        finally:
            if show_status:
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
