"""Atomic publication of completed media files and optional sidecar bytes."""

from __future__ import annotations

import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from .enums import FileStatus, OverwritePolicy


Overwrite = OverwritePolicy | str


def publish_bytes_result(
    data: bytes, destination: Path, *, overwrite: Overwrite
) -> FileStatus:
    """Publish on the same filesystem; return False if another writer won.

    ``skip`` and ``error`` never overwrite existing files, even if another
    process creates a destination after the caller's initial existence check.
    Temporary files are removed after all successes, errors and cancellations.
    """
    if overwrite not in OverwritePolicy._value2member_map_:
        raise ValueError("overwrite must be 'skip', 'replace', or 'error'")
    overwrite = OverwritePolicy(overwrite)
    if destination.exists():
        if overwrite == OverwritePolicy.SKIP:
            return FileStatus.SKIPPED
        if overwrite == OverwritePolicy.ERROR:
            raise FileExistsError(destination)
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
        if overwrite == OverwritePolicy.REPLACE:
            existed = destination.exists()
            staged.replace(destination)
            return FileStatus.OVERWRITTEN if existed else FileStatus.CREATED
        else:
            try:
                os.link(staged, destination)
            except FileExistsError:
                if overwrite == OverwritePolicy.ERROR:
                    raise
                return FileStatus.SKIPPED
            return FileStatus.CREATED
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)


def publish_bytes(data: bytes, destination: Path, *, overwrite: Overwrite) -> bool:
    """Compatibility API: True when this call published a file."""
    return (
        publish_bytes_result(data, destination, overwrite=overwrite)
        != FileStatus.SKIPPED
    )
