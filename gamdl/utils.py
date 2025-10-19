import json

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
