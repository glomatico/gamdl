"""Atomic publication of completed media files and optional sidecar bytes."""

from __future__ import annotations

import ctypes
import errno
import os
import shutil
import sys
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory

from .enums import FileStatus, OverwritePolicy

Overwrite = OverwritePolicy | str

_NO_HARDLINK = {
    errno.EXDEV,
    errno.EPERM,
    errno.EOPNOTSUPP,
    errno.ENOTSUP,
    errno.ENOSYS,
}


def _rename_noreplace(source: Path, destination: Path) -> bool | None:
    """Atomically rename without replacement, if the platform supports it.

    None means the filesystem/kernel cannot provide this operation. Unlike
    os.rename(), neither renameat2 nor renamex_np overwrites an existing file.
    """
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
    if code in _NO_HARDLINK | {errno.EINVAL}:
        return None
    raise OSError(code, os.strerror(code), os.fspath(destination))


def _publish_noreplace(staged: Path, destination: Path) -> bool:
    """Publish a complete staged file without replacing an existing target."""
    try:
        os.link(staged, destination)
    except FileExistsError:
        return False
    except OSError as exc:
        if exc.errno not in _NO_HARDLINK:
            raise
    else:
        return True

    renamed = _rename_noreplace(staged, destination)
    if renamed is not None:
        return renamed

    # Last resort for filesystems without hard links *or* exclusive rename:
    # O_EXCL prevents clobbering another writer, even though the completed
    # file cannot be made visible atomically on such a filesystem.
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
    """Publish an existing staged file atomically using bounded-memory I/O.

    Hardlink completed media on the same filesystem, otherwise stage a chunked
    copy beside the destination. Never publish incomplete files. Keep the
    source intact so filename-collision retries can reuse it.
    """
    if overwrite not in OverwritePolicy._value2member_map_:
        raise ValueError("overwrite must be 'skip' or 'replace'")
    overwrite = OverwritePolicy(overwrite)
    if destination.exists() and overwrite == OverwritePolicy.SKIP:
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    if overwrite == OverwritePolicy.REPLACE:
        # A private directory gives os.link() a collision-free destination.
        # Atomically rename the new hardlink over the old file; unlike a
        # second full-file copy, this is O(1) on the same filesystem.
        with TemporaryDirectory(prefix=".gamdl-", dir=destination.parent) as directory:
            staged = Path(directory) / "media"
            try:
                os.link(source, staged)
            except OSError as exc:
                if exc.errno not in _NO_HARDLINK:
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
            return False
        except OSError as exc:
            if exc.errno not in _NO_HARDLINK:
                raise
        else:
            return True
        # A custom temp_path may be on another filesystem: copy in chunks.
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
        return _publish_noreplace(staged, destination)
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)
