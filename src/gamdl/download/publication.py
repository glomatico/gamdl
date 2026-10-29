"""Atomic publication of completed media files and optional sidecar bytes."""

from __future__ import annotations

import errno
import os
import shutil
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory

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


def publish_file(source: Path, destination: Path, *, overwrite: Overwrite) -> bool:
    """Publish an existing staged file atomically using bounded-memory I/O.

    Hardlink completed media on the same filesystem, otherwise stage a chunked
    copy beside the destination. Never publish incomplete files. Keep the
    source intact so filename-collision retries can reuse it.
    """
    if overwrite not in OverwritePolicy._value2member_map_:
        raise ValueError("overwrite must be 'skip', 'replace', or 'error'")
    overwrite = OverwritePolicy(overwrite)
    if destination.exists():
        if overwrite == OverwritePolicy.SKIP:
            return False
        if overwrite == OverwritePolicy.ERROR:
            raise FileExistsError(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if overwrite == OverwritePolicy.REPLACE:
        # A private directory gives os.link() a collision-free destination.
        # Atomically rename the new hardlink over the old file; unlike a
        # second full-file copy, this is O(1) on the same filesystem.
        with TemporaryDirectory(
            prefix=f".{destination.name}.", dir=destination.parent
        ) as directory:
            staged = Path(directory) / "media"
            try:
                os.link(source, staged)
            except OSError as exc:
                if exc.errno not in {
                    errno.EXDEV,
                    errno.EPERM,
                    errno.EOPNOTSUPP,
                    errno.ENOTSUP,
                }:
                    raise
                with source.open("rb") as stream, staged.open("xb") as output:
                    shutil.copyfileobj(stream, output, length=1024 * 1024)
            staged.replace(destination)
        return True
    if overwrite != OverwritePolicy.REPLACE:
        # Same-filesystem staging can publish the completed source without a
        # second media copy, while still handling a competing writer safely.
        try:
            os.link(source, destination)
        except FileExistsError:
            if overwrite == OverwritePolicy.ERROR:
                raise
            return False
        except OSError as exc:
            if exc.errno not in {
                errno.EXDEV,
                errno.EPERM,
                errno.EOPNOTSUPP,
                errno.ENOTSUP,
            }:
                raise
        else:
            return True
        # A custom temp_path may be on another filesystem: copy in chunks.
    staged: Path | None = None
    try:
        with NamedTemporaryFile(
            mode="wb",
            prefix=f".{destination.name}.",
            suffix=".part",
            dir=destination.parent,
            delete=False,
        ) as output:
            staged = Path(output.name)
            with source.open("rb") as stream:
                shutil.copyfileobj(stream, output, length=1024 * 1024)
        if overwrite == OverwritePolicy.REPLACE:
            staged.replace(destination)
            return True
        try:
            os.link(staged, destination)
        except FileExistsError:
            if overwrite == OverwritePolicy.ERROR:
                raise
            return False
        return True
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)
