"""Convert the numbered chapters of an HTML course archive to text-first Markdown.

The source ZIP remains in imports/. SVG and image content is marked as pending.
This is an import aid, not a visual or scientific validation step.
"""
from __future__ import annotations

import argparse
import hashlib
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
import re
import zipfile


class CourseHTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[str] = []
        self.active: str | None = None
        self.parts: list[str] = []
        self.skip: list[str] = []
        self.visuals = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs = dict(attrs)
        if self.skip:
            if tag == self.skip[-1]:
                self.skip.append(tag)
            return
        if tag in {"style", "script", "footer", "nav", "svg"}:
            if tag == "svg":
                self.visuals += 1
                self.blocks.append(f"[SVG illustration {self.visuals}: visual content pending]")
            self.skip.append(tag)
            return
        if tag == "img":
            self.visuals += 1
            alt = (attrs.get("alt") or "").strip()
            self.blocks.append(f"[Image {self.visuals}: visual content pending; alt text: {alt}]")
            return
        if tag == "br" and self.active:
            self.parts.append("\n")
        if self.active == "tr" and tag in {"td", "th"}:
            self.parts.append(" | ")
        if self.active is None and (tag in {"h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "tr", "pre"}
                                    or tag == "div" and "formula" in (attrs.get("class") or "").split()):
            self.active = "formula" if tag == "div" else tag
            self.parts = []

    def handle_endtag(self, tag: str) -> None:
        if self.skip:
            if tag == self.skip[-1]:
                self.skip.pop()
            return
        if self.active is None:
            return
        if tag != ("div" if self.active == "formula" else self.active):
            return
        raw = "".join(self.parts)
        value = (raw.replace("\r\n", "\n").replace("\r", "\n").strip()
                 if self.active == "pre" else re.sub(r"\s+", " ", raw).strip())
        if value:
            if self.active.startswith("h"):
                value = "#" * int(self.active[1]) + " " + value
            elif self.active == "li":
                value = "- " + value
            elif self.active == "tr":
                value = "| " + value.strip(" |") + " |"
            elif self.active == "pre":
                value = "```text\n" + value + "\n```"
            elif self.active == "formula":
                value = "Formula: " + value
            self.blocks.append(value)
        self.active = None
        self.parts = []

    def handle_data(self, data: str) -> None:
        if self.active and not self.skip:
            self.parts.append(data)


def convert(archive: Path, output: Path) -> list[tuple[str, int, int]]:
    output.mkdir(parents=True, exist_ok=True)
    report = []
    with zipfile.ZipFile(archive) as bundle:
        for entry in sorted(bundle.infolist(), key=lambda x: x.filename):
            path = PurePosixPath(entry.filename)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("Unsafe archive path")
            if not re.fullmatch(r"\d{2}\.html", path.name):
                continue
            raw = bundle.read(entry)
            parser = CourseHTML()
            parser.feed(raw.decode("utf-8-sig"))
            body = "\n\n".join(parser.blocks)
            if not body:
                raise ValueError(f"No text in {path.name}")
            header = (f"<!-- Source archive: {archive.name}; entry: {entry.filename}; "
                      f"HTML SHA256: {hashlib.sha256(raw).hexdigest()} -->\n\n"
                      "Material type: course notes. The title mentions ASML, but this archive does not establish ASML publication or endorsement.\n\n"
                      "This file was converted from HTML to text. Verify graphics, formula layout, and table structure against the original HTML.\n\n")
            (output / (path.stem + ".md")).write_text(header + body + "\n", encoding="utf-8")
            report.append((path.name, len(body), parser.visuals))
    return report


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("archive", type=Path)
    cli.add_argument("output", type=Path)
    args = cli.parse_args()
    for name, characters, visuals in convert(args.archive, args.output):
        print(f"{name}: {characters} text characters, {visuals} visuals pending")
