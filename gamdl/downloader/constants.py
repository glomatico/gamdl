TEMP_PATH_TEMPLATE = "gamdl_temp_{}"
ILLEGAL_CHARS_RE = r'[\\/:*?"<>|;]'
ILLEGAL_CHAR_REPLACEMENT = "_"

# Full-width Unicode replacements for forbidden Windows filename characters.
# Used by _sanitize_string when use_fullwidth_replacements=True (default).
FULLWIDTH_REPLACEMENTS = {
    "\\": "＼",
    "/": "／",
    ":": "：",
    "*": "＊",
    "?": "？",
    '"': "＂",
    "<": "＜",
    ">": "＞",
    "|": "｜",
    ";": "；",
}
