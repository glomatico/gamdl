import colorama


def color_text(text: str, color) -> str:
    return color + text + colorama.Style.RESET_ALL
