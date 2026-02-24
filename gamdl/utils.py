import asyncio
import json
import string
import subprocess
import typing

import httpx


def raise_for_status(httpx_response: httpx.Response, valid_responses: set[int] = {200}):
    if httpx_response.status_code not in valid_responses:
        raise httpx._exceptions.HTTPError(
            f"HTTP error {httpx_response.status_code}: {httpx_response.text}"
        )


def safe_json(httpx_response: httpx.Response) -> dict | None:
    try:
        return httpx_response.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


async def get_response(
    url: str,
    valid_responses: set[int] = {200},
) -> httpx.Response:
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.get(url)
        raise_for_status(response, valid_responses)
        return response


async def async_subprocess(*args: str, silent: bool = False) -> None:
    if silent:
        additional_args = {
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
    else:
        additional_args = {}

    proc = await asyncio.create_subprocess_exec(
        *args,
        **additional_args,
    )
    await proc.communicate()

    if proc.returncode != 0:
        raise Exception(f'"{args[0]}" exited with code {proc.returncode}')


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


async def sequential_gather(
    *tasks: typing.Awaitable[typing.Any],
    interval: float = 0.5,
) -> list[typing.Any]:
    results = []
    for i, task in enumerate(tasks):
        try:
            result = await task
            results.append(result)
        except Exception as e:
            results.append(e)
        if interval > 0 and i < len(tasks) - 1:
            await asyncio.sleep(interval)
    return results


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
