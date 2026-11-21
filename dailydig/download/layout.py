"""Library path layout. Configurable so DailyDig's files look exactly like the rest of your library."""
from __future__ import annotations

import os
import re
import string
import unicodedata

_BAD = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


def to_ascii(s: str) -> str:
    try:
        from unidecode import unidecode  # optional
        return unidecode(s)
    except ImportError:
        return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()


def safe_segment(v: str, ascii_only: bool = False, limit: int = 120) -> str:
    v = to_ascii(v) if ascii_only else v
    v = _BAD.sub("_", v).strip(" .")
    return (v[:limit].rstrip(" .")) or "_"


class _Fields(dict):
    def __missing__(self, key):
        return ""


def render(template: str, fields: dict, root: str, ascii_only: bool = False) -> str:
    """Render `template` under `root`. Values are sanitised *before* formatting, so a '/' inside an
    artist name can never create a directory, and the result can never escape `root`."""
    clean = {}
    for k, v in fields.items():
        clean[k] = v if isinstance(v, int) else safe_segment(str(v), ascii_only) if v not in (None, "") else ""
    try:
        rel = string.Formatter().vformat(template, (), _Fields(clean))
    except (ValueError, IndexError, KeyError) as e:
        raise ValueError(f"bad DD_PATH_TEMPLATE {template!r}: {e}") from e
    parts = [p for p in rel.replace("\\", "/").split("/") if p not in ("", ".", "..")]
    if not parts:
        raise ValueError("path template produced an empty path")
    parts = [safe_segment(p, ascii_only, 150) if i < len(parts) - 1 else p for i, p in enumerate(parts)]
    path = os.path.join(root, *parts)
    if os.path.commonpath([os.path.abspath(path), os.path.abspath(root)]) != os.path.abspath(root):
        raise ValueError("path template escapes the library root")
    return path
