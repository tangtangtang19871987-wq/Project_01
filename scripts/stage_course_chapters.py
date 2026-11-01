"""Copy selected converted course chapters into the knowledge source directory."""
from __future__ import annotations

import argparse
from pathlib import Path
import re
import shutil

from iltkb.storage import atomic_json


def stage(prepared: Path, raw: Path, chapters: list[str]) -> None:
    raw.mkdir(parents=True, exist_ok=True)
    for number in chapters:
        if not re.fullmatch(r"\d{2}", number):
            raise ValueError(f"Invalid chapter number: {number}")
        source = prepared / f"{number}.md"
        if not source.is_file():
            raise ValueError(f"Converted chapter missing: {number}")
        target = raw / source.name
        shutil.copyfile(source, target)
        title = next((line[2:] for line in source.read_text(encoding="utf-8").splitlines()
                      if line.startswith("# ")), f"Lecture {number}")
        atomic_json(target.with_suffix(".md.meta.json"), {
            "source_id": f"asml-course-{number}",
            "title": f"Course lecture {number}: {title}",
            "origin_group": "asml-opc-course-archive-2026",
            "status": "active",
        })


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("prepared", type=Path)
    cli.add_argument("raw", type=Path)
    cli.add_argument("chapters", nargs="+")
    args = cli.parse_args()
    stage(args.prepared, args.raw, args.chapters)
