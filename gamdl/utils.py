import asyncio
import string
import typing


async def async_subprocess(*args: str, silent: bool = False) -> None:
    if silent:
        additional_args = {
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.PIPE,
        }
    else:
        additional_args = {}

    proc = await asyncio.create_subprocess_exec(
        *args,
        **additional_args,
    )

    stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        msg = f'"{args[0]}" exited with code {proc.returncode}'

        if stdout:
            msg += f"\nstdout:\n{stdout.decode()}"
        if stderr:
            msg += f"\nstderr:\n{stderr.decode()}"

        raise Exception(msg)


async def safe_gather(
    *tasks: typing.Awaitable[typing.Any],
    limit: int = 10,
) -> list[typing.Any]:
    semaphore = asyncio.Semaphore(limit)

    async def bounded_task(task: typing.Awaitable[typing.Any]) -> typing.Any:
        async with semaphore:
            return await task

    return await asyncio.gather(
        *(bounded_task(task) for task in tasks),
        return_exceptions=True,
    )


class CustomStringFormatter(string.Formatter):
    def format_field(self, value: typing.Any, format_spec: str) -> str:
        if isinstance(value, tuple) and len(value) == 2:
            actual_value, fallback_value = value
            if actual_value is None:
                return fallback_value

            try:
                return super().format_field(actual_value, format_spec)
            except Exception:
                return fallback_value

        return super().format_field(value, format_spec)


class GamdlError(Exception):
    pass
