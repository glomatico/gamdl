"""Publish completed media through ordinary copies and atomic renames."""

from __future__ import annotations

import ctypes
import errno
import os
import shutil
import sys
from pathlib import Path
from tempfile import NamedTemporaryFile

from .enums import FileStatus, OverwritePolicy

Overwrite = OverwritePolicy | str


def _rename_noreplace(source: Path, destination: Path) -> bool | None:
    """Atomically rename without replacement, if the platform supports it.

    None means the filesystem/kernel cannot provide this operation. Unlike
    os.rename(), neither renameat2 nor renamex_np overwrites an existing file.
    """
    if sys.platform == "win32":
        # On Windows os.rename fails if the destination exists (unlike Unix).
        try:
            source.rename(destination)
        except FileExistsError:
            return False
        return True
    if sys.platform not in {"linux", "darwin"}:
        return None
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        if sys.platform == "darwin":
            rename = libc.renamex_np
            rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
            arguments = (os.fsencode(source), os.fsencode(destination), 0x4)
        else:
            rename = libc.renameat2
            rename.argtypes = [
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_uint,
            ]
            arguments = (-100, os.fsencode(source), -100, os.fsencode(destination), 1)
    except AttributeError:
        return None
    rename.restype = ctypes.c_int
    if rename(*arguments) == 0:
        return True
    code = ctypes.get_errno()
    if code in {errno.EEXIST, errno.ENOTEMPTY}:
        return False
    if code in {errno.ENOSYS, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EINVAL}:
        return None
    raise OSError(code, os.strerror(code), os.fspath(destination))


def _publish_noreplace(staged: Path, destination: Path) -> bool:
    """Publish a complete staged file without replacing an existing target."""
    renamed = _rename_noreplace(staged, destination)
    if renamed is not None:
        return renamed

    # Fallback for filesystems without exclusive rename. O_EXCL prevents
    # replacing an existing file, but cannot guarantee atomic visibility.
    try:
        output = destination.open("xb")
    except FileExistsError:
        return False
    try:
        with output, staged.open("rb") as stream:
            shutil.copyfileobj(stream, output, length=1024 * 1024)
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    return True


def publish_bytes_result(
    data: bytes, destination: Path, *, overwrite: Overwrite
) -> FileStatus:
    """Publish on the same filesystem; return False if another writer won.

    ``skip`` never overwrites existing files, even if another
    process creates a destination after the caller's initial existence check.
    Temporary files are removed after all successes, errors and cancellations.
    """
    if overwrite not in OverwritePolicy._value2member_map_:
        raise ValueError("overwrite must be 'skip' or 'replace'")
    overwrite = OverwritePolicy(overwrite)
    if destination.exists() and overwrite == OverwritePolicy.SKIP:
        return FileStatus.SKIPPED
    destination.parent.mkdir(parents=True, exist_ok=True)
    staged: Path | None = None
    try:
        with NamedTemporaryFile(
            mode="wb",
            # The published filename may already occupy all 255 UTF-8 bytes
            # allowed by the filesystem. A short, independent staging name
            # leaves room for tempfile's random suffix and avoids ENAMETOOLONG.
            prefix=".gamdl-",
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
            return (
                FileStatus.CREATED
                if _publish_noreplace(staged, destination)
                else FileStatus.SKIPPED
            )
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
    """Copy media beside its destination, then rename the completed copy.

    The original source remains available for retries and other callers.
    Never expose a partial copy on filesystems supporting atomic rename.
    """
    if overwrite not in OverwritePolicy._value2member_map_:
        raise ValueError("overwrite must be 'skip' or 'replace'")
    overwrite = OverwritePolicy(overwrite)
    if destination.exists() and overwrite == OverwritePolicy.SKIP:
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    staged: Path | None = None
    try:
        with NamedTemporaryFile(
            mode="wb",
            prefix=".gamdl-",
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
        return _publish_noreplace(staged, destination)
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)
