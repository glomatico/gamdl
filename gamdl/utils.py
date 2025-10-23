import json
import typing
import subprocess
import asyncio

import httpx


def raise_for_status(httpx_response: httpx.Response, valid_responses: set[int] = {200}):
    if httpx_response.status_code not in valid_responses:
        raise httpx._exceptions.HTTPError(
            f"HTTP error {httpx_response.status_code}: {httpx_response.text}"
        )


def safe_json(httpx_response: httpx.Response) -> dict:
    try:
        return httpx_response.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}


async def get_response_text(url: str) -> str:
    async with httpx.AsyncClient() as client:
        response = await client.get(url)
        raise_for_status(response)
        return response.text


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
    limit: int = 5,
    retries: int = 3,
) -> list[typing.Any]:
    semaphore = asyncio.Semaphore(limit)

    async def bounded_task(task: typing.Awaitable[typing.Any]) -> typing.Any:
        async with semaphore:
            last_exception = None
            for attempt in range(retries + 1):
                try:
                    return await task
                except Exception as e:
                    last_exception = e
                    if attempt < retries:
                        await asyncio.sleep(2**attempt)
            return last_exception

    return await asyncio.gather(
        *(bounded_task(task) for task in tasks),
        return_exceptions=True,
    )
