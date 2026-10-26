"""Atomic publication of completed media files and optional sidecar bytes."""

from __future__ import annotations

import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Literal


Overwrite = Literal["skip", "replace", "error"]


def publish_bytes(data: bytes, destination: Path, *, overwrite: Overwrite) -> bool:
    """Publish on the same filesystem; return False if another writer won.

    ``skip`` and ``error`` never overwrite existing files, even if another
    process creates a destination after the caller's initial existence check.
    Temporary files are removed after all successes, errors and cancellations.
    """
    if overwrite not in ("skip", "replace", "error"):
        raise ValueError("overwrite must be 'skip', 'replace', or 'error'")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staged: Path | None = None
    try:
        with NamedTemporaryFile(
            mode="wb",
            prefix=f".{destination.name}.",
            suffix=".part",
            dir=destination.parent,
            delete=False,
        ) as temporary:
            staged = Path(temporary.name)
            temporary.write(data)
        if overwrite == "replace":
            staged.replace(destination)
        else:
            try:
                os.link(staged, destination)
            except FileExistsError:
                if overwrite == "error":
                    raise
                return False
        return True
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)
