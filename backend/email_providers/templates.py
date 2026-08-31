"""Template rendering for email bodies (issue #27).

Tiny stub-template engine: ``{{var}}`` placeholders are replaced with
``variables[var]`` (or the empty string if missing). We deliberately do NOT
use ``str.format`` because user-supplied templates can contain ``{`` /
``}`` characters that break the format spec — regex is safer here.

The renderer also escapes the value before substitution so a malicious
template can't inject HTML. HTML escaping is conservative; downstream
callers can mark a template as "trusted" via the ``html_safe`` flag.
"""
from __future__ import annotations

import html
import re
from typing import Any

_PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")


def render_template(template: str, variables: dict[str, Any], *, html_safe: bool = True) -> str:
    """Replace every ``{{name}}`` in ``template`` with ``variables.get(name)``.

    Missing variables become the empty string. If ``html_safe`` is True the
    replacement value is HTML-escaped — recommended when the result will be
    embedded inside an HTML body. Set to False for plain-text subjects.
    """
    def _sub(match: re.Match) -> str:
        key = match.group(1)
        value = variables.get(key, "")
        s = "" if value is None else str(value)
        return html.escape(s) if html_safe else s

    return _PLACEHOLDER_RE.sub(_sub, template or "")


def extract_variables(template: str) -> list[str]:
    """Return the unique variable names referenced by ``{{...}}`` in the
    template. Used by the API to advertise the inputs a template requires."""
    seen: list[str] = []
    for m in _PLACEHOLDER_RE.finditer(template or ""):
        name = m.group(1)
        if name not in seen:
            seen.append(name)
    return seen
