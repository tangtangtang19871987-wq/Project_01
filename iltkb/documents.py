"""Lossless text spans and explicit visual coverage gaps."""
from __future__ import annotations

import re
from pathlib import Path

PARSER_VERSION = "text-spans-v2"


def load_document(path: Path, pdf_pages: list[int] | None = None) -> tuple[bytes, str, list[dict], list[dict]]:
    original = path.read_bytes()
    if path.suffix.lower() == ".pdf":
        try:
            import pymupdf
        except ImportError:
            raise ValueError("PDF input requires: pip install '.[pdf]'") from None
        parts, pages, pending = [], [], []
        offset = 0
        with pymupdf.open(stream=original, filetype="pdf") as doc:
            if doc.needs_pass:
                raise ValueError(f"Encrypted PDF: {path.name}")
            selected = pdf_pages if pdf_pages is not None else list(range(1, len(doc) + 1))
            if (not selected or any(type(n) is not int or n < 1 or n > len(doc) for n in selected)
                    or selected != sorted(set(selected))):
                raise ValueError("pdf_pages must be sorted, unique, in-range 1-based page numbers")
            for number in selected:
                page = doc[number - 1]
                part = page.get_text(sort=True) + "\n"
                parts.append(part)
                pages.append({"page": number, "start": offset, "end": offset + len(part)})
                offset += len(part)
                pending.append({"page": number,
                                "reason": "Figures, scanned content, table layout and formula fidelity require visual review"})
        return original, "".join(parts), pages, pending
    if pdf_pages is not None:
        raise ValueError("pdf_pages metadata applies only to PDF sources")
    text = original.decode("utf-8")
    pending = [{"offset": m.start(), "reason": "Markdown image requires visual review", "reference": m.group(0)}
               for m in re.finditer(r"!\[[^\]]*\]\([^\n]*?\)|<img\b[^>]*>", text, re.I)]
    # Reference-style images must be tracked too.
    pending += [{"offset": m.start(), "reason": "Markdown reference image requires visual review", "reference": m.group(0)}
                for m in re.finditer(r"!\[[^\]]*\](?:\[[^\]]*\])?(?!\()", text)]
    imported_visual = (r"^\[(?:SVG illustration|Image) \d+: visual content pending[^\]]*\]$"
                       r"|^\[(?:SVG \u56fe\u793a|\u56fe\u50cf) \d+\uff1a"
                       r"\u89c6\u89c9\u5185\u5bb9\u5f85\u5904\u7406[^\]]*\]$")
    pending += [{"offset": m.start(), "reason": "Imported HTML visual requires visual review", "reference": m.group(0)}
                for m in re.finditer(imported_visual, text, re.M)]
    return original, text, [], pending


def chunks(text: str, max_chars: int = 9000, coalesce_sections: bool = False) -> list[dict]:
    """Preserve every character, including long lines; don't treat fenced code as headings."""
    if max_chars < 1:
        raise ValueError("chunk_chars must be positive")
    boundaries = [(0, "Document")]
    offset, stack, fence = 0, [], None
    for line in text.splitlines(keepends=True):
        marker = re.match(r"^\s*(`{3,}|~{3,})", line)
        if marker:
            token = marker.group(1)
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
        heading = re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", line)
        if heading and fence is None:
            level, name = len(heading.group(1)), heading.group(2)
            stack = stack[:level - 1] + [name]
            if offset:
                boundaries.append((offset, " / ".join(stack)))
            else:
                boundaries[0] = (0, " / ".join(stack))
        offset += len(line)
    output = []
    if coalesce_sections:
        start = end = 0
        sections = []

        def flush():
            if end > start:
                output.append({"start": start, "end": end, "section": sections[0]["section"],
                               "sections": [{"offset": row["start"] - start, "section": row["section"]}
                                            for row in sections], "text": text[start:end]})

        for index, (section_start, heading) in enumerate(boundaries):
            section_end = boundaries[index + 1][0] if index + 1 < len(boundaries) else len(text)
            for pos in range(section_start, section_end, max_chars):
                piece_end = min(pos + max_chars, section_end)
                if end > start and piece_end - start > max_chars:
                    flush()
                    start = end = pos
                    sections = []
                if end == start:
                    start = pos
                    sections = []
                sections.append({"start": pos, "section": heading})
                end = piece_end
        flush()
        return output
    for index, (start, heading) in enumerate(boundaries):
        end = boundaries[index + 1][0] if index + 1 < len(boundaries) else len(text)
        for pos in range(start, end, max_chars):
            output.append({"start": pos, "end": min(pos + max_chars, end), "section": heading,
                           "text": text[pos:min(pos + max_chars, end)]})
    return output
