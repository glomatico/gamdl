"""Interactive first-use configuration of the user's music library location."""

from __future__ import annotations

from pathlib import Path

import click
from platformdirs import user_music_path

from .config.loader import ConfigError, save_output_directory
from .prompts import interactive
from .tui.components import MenuChoice
from .tui.wizard import FullScreenWizard, WizardBack


async def resolve_output_directory(configured: str | None) -> str:
    """Return an existing preference, or prompt once and persist the selection.

    Explicit CLI paths never trigger or overwrite saved preferences. An
    unconfigured non-interactive invocation must fail before downloading.
    """
    if configured is not None:
        return str(Path(configured).expanduser().absolute())
    if not interactive():
        raise click.ClickException(
            "Output directory is not configured. Set output.directory in your "
            "config or pass --output-dir PATH; interactive setup requires a terminal."
        )

    suggested = str(user_music_path())
    wizard = FullScreenWizard(summary="Music library setup")

    async def flow(ui: FullScreenWizard) -> str:
        while True:
            try:
                chosen = await ui.ask(
                    "Choose your music folder",
                    "Save downloaded music to",
                    description="Press Enter to use your system's Music directory, "
                    "or enter another folder.",
                    default=suggested,
                )
            except WizardBack:
                continue

            raw = chosen.strip().strip('"').strip("'").strip()
            if not raw:
                raw = suggested
            destination = Path(raw).expanduser().absolute()
            try:
                destination.mkdir(parents=True, exist_ok=True)
                if not destination.is_dir():
                    raise NotADirectoryError(destination)
            except OSError:
                await ui.choose(
                    "Invalid music folder",
                    [MenuChoice("Choose another folder", 0)],
                    description="The directory could not be created or accessed.",
                )
                continue

            root_ctx = click.get_current_context(silent=True)
            root = root_ctx.find_root().obj if root_ctx is not None else None
            if root is not None and not root.get("no_config", False):
                try:
                    save_output_directory(root["config_path"], str(destination))
                except ConfigError:
                    await ui.choose(
                        "Unable to save music folder",
                        [MenuChoice("Try again", 0)],
                        description="Unable to save configuration. Check file permissions.",
                    )
                    continue
            return str(destination)

    return await wizard.run(flow)
