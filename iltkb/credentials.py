"""Read one named credential; never source a shell file or mutate the environment."""
from __future__ import annotations

import os
from pathlib import Path
import re


def resolve_api_key(name: str, env_file: Path | None = None) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise ValueError("api_key_env must be an environment variable name")
    existing = os.environ.get(name)
    if existing and existing.strip():
        return existing
    if env_file is None:
        raise ValueError(f"Set {name} in the process environment or configure env_file; do not put API keys in iltkb.json")
    try:
        lines = env_file.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError):
        raise ValueError("Cannot read configured env_file on this machine; check its path and permissions") from None
    found = None
    for number, line in enumerate(lines, 1):
        match = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$", line)
        if not match or match[1] != name:
            continue
        if found is not None:
            raise ValueError(f"Duplicate {name} entries in env_file")
        raw = match[2].strip()
        if raw.startswith(('"', "'")):
            pattern = r'''("(?:[^"\\]|\\.)*"|'[^']*')\s*(?:#.*)?'''
            quoted = re.fullmatch(pattern, raw)
            if quoted is None:
                raise ValueError(f"Unsupported credential quoting in env_file at line {number}; use a single-line value")
            literal = quoted[1]
            found = literal[1:-1]
            if literal[0] == '"':
                escapes = {'n': '\n', 'r': '\r', 't': '\t', '"': '"', '\\': '\\'}
                found = re.sub(r'\\([nrt"\\])', lambda m: escapes[m[1]], found)
        else:
            found = re.split(r"\s+#", raw, maxsplit=1)[0].strip()
    if not found or not found.strip():
        raise ValueError(f"env_file has no nonempty {name} entry")
    return found
