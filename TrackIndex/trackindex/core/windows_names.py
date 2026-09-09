from __future__ import annotations

import re

_INVALID_WINDOWS_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED_WINDOWS_STEMS = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}


def windows_name_error(name: str, *, max_length: int = 255) -> str:

    value = str(name)
    if not value:
        return "The name is empty."
    if len(value) > max_length:
        return f"The name is longer than {max_length} characters."
    if value[-1] in {" ", "."} or _INVALID_WINDOWS_CHARS.search(value):
        return "The name contains characters Windows cannot use."


    if value.split(".", 1)[0].casefold() in _RESERVED_WINDOWS_STEMS:
        return "The name is reserved by Windows."
    return ""


def is_valid_windows_name(name: str, *, max_length: int = 255) -> bool:
    return not windows_name_error(name, max_length=max_length)
