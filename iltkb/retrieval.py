"""Chinese lexical recall and version-pinned evidence reading."""
from __future__ import annotations

import json
from pathlib import Path
import re

from .engine import require
from .incidents import require_clear
from .storage import current_release, read_json


def tokens(text: str) -> set[str]:
    result = set(re.findall(r"[a-z0-9_]+", text.lower()))
    for run in re.findall(r"[\u3400-\u9fff]+", text):
        if len(run) == 1:
            result.add(run)
        else:
            result.update(run[i:i + 2] for i in range(len(run) - 1))
    return result


class Reader:
    def __init__(self, root: Path, *, allow_quarantine: bool = False):
        self.root = root.resolve()
        self.allow_quarantine = allow_quarantine
        self._check_quarantine()
        self.release = current_release(self.root)
        require(self.release is not None, "No published build; run build first")
        # Pin this pointer once, including subsequent reads and answer construction.
        self.manifest = read_json(self.release / "manifest.json")

    def _check_quarantine(self) -> None:
        if not self.allow_quarantine:
            require_clear(self.root)

    def search(self, question: str, limit: int = 5) -> list[dict]:
        self._check_quarantine()
        require(limit > 0, "Search limit must be positive")
        query = tokens(question)
        for page in self.manifest["pages"].values():
            vocab = page["topic"]["aliases"] + page["topic"]["keywords"]
            if any(term.lower() in question.lower() for term in vocab if term):
                for term in vocab:
                    query |= tokens(term)
        results = []
        for tid, page in self.manifest["pages"].items():
            if not page["claims"]:
                continue
            topic = page["topic"]
            title_tokens = tokens(topic["title"] + " " + " ".join(topic["keywords"] + topic["aliases"] + page["keywords"]))
            body_tokens = tokens(page["summary"] + " ".join(c["text"] for c in page["claims"]))
            score = len(query & title_tokens) * 4 + len(query & body_tokens)
            if score:
                results.append({"topic": tid, "title": topic["title"], "score": score,
                                "summary": page["summary"], "group": topic["group"],
                                "path": f"wiki/{topic['group']}/{tid}.md"})
        return sorted(results, key=lambda x: (-x["score"], x["topic"]))[:limit]

    def context(self, question: str, limit: int = 5) -> dict:
        self._check_quarantine()
        matches = self.search(question, limit)
        pages = {m["topic"]: self.manifest["pages"][m["topic"]] for m in matches}
        refs = {e for p in pages.values() for c in p["claims"] for e in c["evidence"]}
        evidence = {e: self.manifest["evidence"][e] for e in sorted(refs)}
        sources = {e["source_id"]: self.manifest["sources"][e["source_id"]] for e in evidence.values()}
        return {"question": question, "release": self.release.name, "topics": pages,
                "evidence": evidence, "sources": sources, "scope": "text-only"}

    def answer(self, question: str, model, limit: int = 5) -> dict:
        self._check_quarantine()
        context = self.context(question, limit)
        warning = "Text mode: images, chart layout, and formula fidelity still need visual review."
        if not context["evidence"]:
            return {"release": self.release.name, "answer": warning + "\nNot enough knowledge pages were found. Broaden the query or add sources.", "evidence": []}
        request = {**context, "citation_format": (
            "Every citation bracket contains exactly ONE original evidence ID, "
            "for example [E:E123][E:E456]. Never put commas in one bracket. "
            "Never cite claim IDs beginning C. Cite material limitations too."
        )}
        for attempt in range(3):
            result = model.complete("answer", request)
            answer = result.get("answer")
            refs = result.get("evidence")
            if isinstance(answer, str) and answer.strip() and isinstance(refs, list) and all(isinstance(e, str) for e in refs):
                raw_citations = re.findall(r"\[E:([^\]]*)\]", answer)
                citations = set(raw_citations)
                if (raw_citations and all(re.fullmatch(r"[A-Za-z0-9_-]+", cite)
                                          and cite in context["evidence"] for cite in raw_citations)):
                    # Inline citations are the public claim references; ignore an inconsistent
                    # auxiliary evidence list rather than publishing uncited IDs.
                    self._check_quarantine()
                    return {"release": self.release.name, "answer": warning + "\n" + answer,
                            "evidence": sorted(citations)}
            if attempt < 2:
                request = {**request, "citation_correction": (
                    "Return a corrected answer. Cite each evidence ID separately in the exact "
                    "form [E:E123] using IDs from evidence; do not combine IDs inside one "
                    "bracket and never use a claim ID. The evidence array must list exactly "
                    "the cited IDs. Check EVERY [E:...] bracket before returning."
                )}
        raise ValueError("Unknown/missing inline citations")
