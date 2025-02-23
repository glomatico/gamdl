import colorama
import requests


def color_text(text: str, color) -> str:
    return color + text + colorama.Style.RESET_ALL


def raise_response_exception(response: requests.Response):
    raise Exception(
        f"Request failed with status code {response.status_code}: {response.text}"
    )
