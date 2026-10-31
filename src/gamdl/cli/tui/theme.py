"""Shared Gamdl full-screen terminal color palette."""

from prompt_toolkit.styles import Style


THEME = Style.from_dict(
    {
        "heading": "bold #69b5ff",
        "muted": "#8894a6",
        "focused": "reverse bold",
        "action": "bold #63d89c",
        "selected": "#63d89c",
        "detail": "#aebdd1",
        "error": "#ff7777",
    }
)
