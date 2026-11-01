"""File-based, evidence-grounded compilation, orchestrated by LangGraph."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from difflib import SequenceMatcher
import json
from pathlib import Path
import re
from typing import TypedDict
import unicodedata
import uuid

from .documents import PARSER_VERSION, chunks, load_document
from .storage import atomic_json, current_release, digest, read_json, writer_lock

DEFAULT_TOPICS = [
    {"id": "imaging", "title": "Imaging and process models", "group": "models", "keywords": ["NA", "optics", "photoresist", "process window"], "aliases": ["imaging", "process window", "illumination"]},
    {"id": "optimization", "title": "ILT objectives and optimization", "group": "methods", "keywords": ["EPE", "gradient", "objective function", "solver"], "aliases": ["inverse lithography", "ILT", "mask optimization"]},
    {"id": "manufacturing", "title": "Mask manufacturing constraints", "group": "methods", "keywords": ["MRC", "curvilinear mask", "minimum feature"], "aliases": ["manufacturability", "mask constraints"]},
    {"id": "evaluation", "title": "Experiments and performance evaluation", "group": "evaluation", "keywords": ["dataset", "runtime", "hardware", "error"], "aliases": ["benchmark", "experimental comparison"]},
    {"id": "other", "title": "Topics to classify", "group": "other", "keywords": [], "aliases": []},
]
GENERATED_MARKER = "<!-- iltkb-generated: never an independent source -->"
INDEPENDENCE_RULE_VERSION = "single-source-v2"


def initialize(root: Path, provider: str = "gemini") -> None:
    require(provider in ("gemini", "ollama"), "Unknown model provider")
    root.mkdir(parents=True, exist_ok=True)
    (root / "raw").mkdir(exist_ok=True)
    config = root / "iltkb.json"
    if config.exists():
        raise ValueError("iltkb.json already exists")
    settings = {"provider": provider, "model": "",
                         "model_revision": "set-after-selecting-model",
                         "publication_policy": "review_required",
                         "chunk_chars": 9000, "max_input_chars": 40000,
                         "workers": 1, "topics": DEFAULT_TOPICS}
    settings.update({"api_key_env": "GEMINI_API_KEY", "max_output_tokens": 8192} if provider == "gemini"
                    else {"base_url": "http://localhost:11434", "num_ctx": 32768})
    atomic_json(config, settings)
    from .ontology import initialize_schema
    initialize_schema(root)
    (root / "AGENTS.md").write_text(
        "# ILT knowledge reading protocol\n\n"
        "Pin .iltkb/current.json once per query; all reads use that release.\n"
        "Read wiki/index.md, then group indexes, then topic pages. Expand synonyms and\n"
        "follow related pages. Verify material claims against original evidence records.\n"
        "Source statements, inferences and conflicts differ. Preserve process/model/data/\n"
        "hardware conditions. Generated pages are never independent evidence.\n"
        "PDF figures and Markdown images are pending, not analyzed. Surface these gaps.\n"
        "Use `python -m iltkb search ROOT QUESTION` for multilingual lexical candidates.\n",
        encoding="utf-8")


def require(condition, message: str):
    if not condition:
        raise ValueError(message)


def string_list(value) -> bool:
    return isinstance(value, list) and all(isinstance(x, str) for x in value)


def realign_quote(quote: str, source_text: str) -> str | None:
    """Use an original line only when a failed quote has a long, unique anchor."""
    if quote in source_text:
        return quote
    def canonical(value: str) -> tuple[str, list[int]]:
        chars, positions = [], []
        index = 0
        while index < len(value):
            char = value[index]
            # PDF line wrapping may split one word as "al-\ngorithm".
            if char == "-" and index > 0 and value[index - 1].isalpha():
                tail = re.match(r"-\s*\n\s*([A-Za-z])", value[index:])
                if tail:
                    index += tail.end() - 1
                    continue
            if char.isspace():
                if chars and chars[-1] != " ":
                    chars.append(" ")
                    positions.append(index)
            else:
                for normalized in unicodedata.normalize("NFKC", char):
                    chars.append(normalized)
                    positions.append(index)
            index += 1
        if chars and chars[-1] == " ":
            chars.pop()
            positions.pop()
        return "".join(chars), positions

    wanted, _ = canonical(quote)
    normalized, offsets = canonical(source_text)
    if len(wanted) >= 30:
        match = normalized.find(wanted)
        if match >= 0 and normalized.find(wanted, match + 1) < 0:
            return source_text[offsets[match]:offsets[match + len(wanted) - 1] + 1]
    matches = []
    for line in source_text.splitlines():
        if not line.strip():
            continue
        anchor = SequenceMatcher(None, quote, line, autojunk=False).find_longest_match()
        if anchor.size >= max(30, len(quote) // 2):
            numbers = re.findall(r"\d+(?:\.\d+)?", quote)
            if all(number in line for number in numbers):
                matches.append((anchor.size, line))
    matches.sort(key=lambda row: row[0], reverse=True)
    if not matches or (len(matches) > 1 and matches[0][0] == matches[1][0]):
        return None
    return matches[0][1]


def validate_config(config):
    require(type(config.get("coalesce_sections", False)) is bool, "coalesce_sections must be boolean")
    require(config.get("publication_policy", "review_required") in ("auto", "review_required"),
            "publication_policy must be auto or review_required")
    ids = []
    for topic in config["topics"]:
        require(all(isinstance(topic.get(k), str) and topic[k] for k in ("id", "title", "group")), "Invalid topic")
        require(re.fullmatch(r"[a-z0-9_-]+", topic["id"]) and re.fullmatch(r"[a-z0-9_-]+", topic["group"]), "Unsafe topic path")
        require(string_list(topic.get("keywords")) and string_list(topic.get("aliases")), "Invalid topic vocabulary")
        ids.append(topic["id"])
    require(len(ids) == len(set(ids)) and ids, "Duplicate/empty topics")


class BuildState(TypedDict, total=False):
    root: str
    config: dict
    model: object
    old: dict
    sources: dict
    evidence: dict
    pages: dict
    stage: str
    fingerprint: str
    rebuilt_topics: list


def scan(state: BuildState) -> dict:
    from .incidents import incidents
    root, config = Path(state["root"]), state["config"]
    validate_config(config)
    internal = root / ".iltkb"
    objects = internal / "objects"
    objects.mkdir(parents=True, exist_ok=True)
    previous = current_release(root)
    old = read_json(previous / "manifest.json") if previous else {"sources": {}, "pages": {}, "evidence": {}}
    source_records = {}
    candidates = []
    for path in sorted((root / "raw").rglob("*")):
        require(not path.is_symlink(), f"Symlinks not allowed in raw: {path.name}")
        if path.is_file() and path.suffix.lower() in (".md", ".markdown", ".pdf"):
            meta_file = path.with_suffix(path.suffix + ".meta.json")
            meta = read_json(meta_file) if meta_file.exists() else {}
            selected_pdf_pages = meta.get("pdf_pages")
            original, text, pages, pending = load_document(path, selected_pdf_pages)
            require(GENERATED_MARKER not in text, "Generated ILTKB output cannot be re-ingested as source evidence")
            candidates.append((path, original, text, pages, pending, meta))
    old_paths = {v["path"]: k for k, v in old["sources"].items()}
    quarantined = incidents(root)["active"]
    present_paths = {p.relative_to(root / "raw").as_posix() for p, *_ in candidates}
    for path, original, text, pages, pending, meta in candidates:
        rel = path.relative_to(root / "raw").as_posix()
        sha = digest(original)
        require(meta.get("source_kind", "external") != "generated", "Generated content cannot be an independent source")
        sid = meta.get("source_id") or old_paths.get(rel)
        if not sid:
            # Preserve identity across an unambiguous move; never collapse duplicate files.
            matches = [k for k, v in old["sources"].items()
                       if v["sha256"] == sha and v["path"] not in present_paths and k not in source_records]
            sid = matches[0] if len(matches) == 1 else "S" + uuid.uuid4().hex
        require(isinstance(sid, str) and re.fullmatch(r"[A-Za-z0-9_-]+", sid), "Invalid source_id")
        require(sid not in source_records, "Duplicate source_id; metadata IDs must be unique")
        require(meta.get("origin_group") is None or isinstance(meta["origin_group"], str), "Invalid origin_group")
        require(meta.get("status", "active") in ("active", "withdrawn"), "Invalid source status")
        text_hash = digest(text)
        for hash_value, data in ((sha, original), (text_hash, text.encode())):
            target = objects / hash_value
            if target.exists():
                require(digest(target.read_bytes()) == hash_value, "Corrupted immutable object")
            else:
                target.write_bytes(data)
        source_records[sid] = {"id": sid, "path": rel, "sha256": sha,
                               "text_hash": text_hash, "pages": pages, "visual_pending": pending,
                               "status": ("withdrawn" if sid in quarantined and
                                          sha == quarantined[sid]["source_hash"] else meta.get("status", "active")),
                               "origin_group": meta.get("origin_group"), "title": meta.get("title", path.stem)}
        if selected_pdf_pages := meta.get("pdf_pages"):
            source_records[sid]["pdf_pages_selected"] = selected_pdf_pages
    fp = digest({"model": state["model"].fingerprint, "revision": config.get("model_revision"),
                 "parser": PARSER_VERSION, "topics": config["topics"], "chunk_chars": config["chunk_chars"],
                 "coalesce_sections": config.get("coalesce_sections", False)})
    stage = internal / "staging" / uuid.uuid4().hex
    stage.mkdir(parents=True)
    return {"old": old, "sources": source_records, "fingerprint": fp, "stage": str(stage)}


def extract(state: BuildState) -> dict:
    objects = Path(state["root"]) / ".iltkb" / "objects"
    cache = Path(state["root"]) / ".iltkb" / "cache"
    old, fp = state["old"], state["fingerprint"]
    topic_ids = {t["id"] for t in state["config"]["topics"]}

    def one(pair):
        sid, source = pair
        if source["status"] == "withdrawn":
            text = (objects / source["text_hash"]).read_bytes().decode("utf-8")
            source["coverage"] = [{"start": c["start"], "end": c["end"], "section": c["section"],
                                   "text_hash": digest(c["text"]), "excluded": "source withdrawn"}
                                  for c in chunks(text, state["config"]["chunk_chars"],
                                                  state["config"].get("coalesce_sections", False))]
            return sid, []
        if (old.get("fingerprint") == fp and sid in old["sources"]
                and old["sources"][sid].get("status") == source["status"]
                and old["sources"][sid]["text_hash"] == source["text_hash"]
                and old["sources"][sid]["sha256"] == source["sha256"]):
            source["coverage"] = old["sources"][sid]["coverage"]
            # Metadata changes still trigger synthesis because page inputs include source records.
            return sid, [e for e in old["evidence"].values() if e["source_id"] == sid]
        text = (objects / source["text_hash"]).read_bytes().decode("utf-8")
        evidence, coverage = [], []
        for chunk in chunks(text, state["config"]["chunk_chars"],
                            state["config"].get("coalesce_sections", False)):
            payload = {"source_id": sid, "section": chunk["section"], "text": chunk["text"],
                       "topic_catalog": state["config"]["topics"]}
            if "pdf_pages_selected" in source:
                payload["source_scope"] = "Selected PDF pages only; omitted pages were not analyzed"
                payload["pdf_pages_selected"] = source["pdf_pages_selected"]
            if "sections" in chunk:
                payload["sections"] = chunk["sections"]
            cache_path = cache / (digest([fp, payload]) + ".json")
            cached = read_json(cache_path) if cache_path.exists() else None
            if cached:
                response = cached["response"]
            else:
                invalid_quotes = []
                repairs = []
                for attempt in range(5):
                    request = payload if attempt == 0 else {
                        **payload,
                        "invalid_quotes": invalid_quotes,
                        "retry_instruction": (
                            "Replace every invalid quote with a short, exact substring from ONE "
                            "line of text (prefer 30-120 characters). Never join separate "
                            "bullets or table rows. Split a combined assertion into separate "
                            "items with separate exact quotes. Check every quote character "
                            "against the supplied text before returning JSON."
                        ),
                    }
                    response = state["model"].complete("extract", request)
                    invalid_quotes = ([item["quote"] for item in response.get("items", [])
                                       if isinstance(item, dict) and isinstance(item.get("quote"), str)
                                       and item["quote"].strip() and item["quote"] not in chunk["text"]]
                                      if isinstance(response.get("items"), list) else [])
                    for item in response.get("items", []):
                        quote = item.get("quote") if isinstance(item, dict) else None
                        if isinstance(quote, str) and quote in invalid_quotes:
                            aligned = realign_quote(quote, chunk["text"])
                            if aligned is not None:
                                item["quote"] = aligned
                                repairs.append({"original": quote, "source_line": aligned})
                    invalid_quotes = [item.get("quote") for item in response.get("items", [])
                                      if isinstance(item, dict) and isinstance(item.get("quote"), str)
                                      and item["quote"].strip() and item["quote"] not in chunk["text"]]
                    if not invalid_quotes:
                        break
                    atomic_json(Path(state["stage"]) / "extraction-attempts" /
                                (digest([sid, chunk["start"]]) + f"-{attempt}.json"), response)
                if repairs:
                    atomic_json(Path(state["stage"]) / "quote-realignments" /
                                (digest([sid, chunk["start"]]) + ".json"), repairs)
                require(not invalid_quotes, "Fabricated/non-exact source quote")
            atomic_json(Path(state["stage"]) / "extractions" / (digest([sid, chunk["start"]]) + ".json"),
                        {"source_id": sid, "start": chunk["start"], "end": chunk["end"], "response": response})
            require(isinstance(response.get("items"), list), "Extraction must contain items")
            if not response["items"]:
                require(isinstance(response.get("empty_reason"), str) and response["empty_reason"].strip(), "Empty extraction requires reason")
            for item in response["items"]:
                quote = item.get("quote")
                require(isinstance(quote, str) and quote.strip(), "Empty quote")
                local = chunk["text"].find(quote)
                require(local >= 0, "Fabricated/non-exact source quote")
                if chunk["text"].find(quote, local + 1) >= 0:
                    local = item.get("quote_offset")
                    require(type(local) is int and local >= 0 and chunk["text"][local:local + len(quote)] == quote, "Ambiguous quote needs exact quote_offset")
                require(isinstance(item.get("statement"), str) and item["statement"].strip(), "Missing source statement")
                require(isinstance(item.get("conditions"), dict), "Missing explicit conditions")
                require(string_list(item.get("topics")) and item["topics"] and set(item["topics"]) <= topic_ids, "Invalid extraction topic")
                start = chunk["start"] + local
                section = next((row["section"] for row in reversed(chunk.get("sections", []))
                                if row["offset"] <= local), chunk["section"])
                eid = "E" + digest([sid, source["sha256"], start, quote, item["statement"]])[:24]
                evidence.append({"id": eid, "source_id": sid, "source_hash": source["sha256"],
                                 "start": start, "end": start + len(quote), "quote": quote,
                                 "section": section, "line": text.count("\n", 0, start) + 1,
                                 "page": next((p["page"] for p in source["pages"] if p["start"] <= start < p["end"]), None),
                                 "statement": item["statement"], "conditions": item["conditions"],
                                 "topics": sorted(set(item["topics"])), "kind": "source_statement"})
            review = cached["review"] if cached else state["model"].complete("extract_review", {**payload, "extraction": response})
            require(review.get("coverage_ok") is True and review.get("issues") == [], "Extraction review failed: " + str(review.get("issues")))
            if not cached:
                atomic_json(cache_path, {"response": response, "review": review})
            coverage.append({"start": chunk["start"], "end": chunk["end"], "section": chunk["section"],
                             "text_hash": digest(chunk["text"]), "empty_reason": response.get("empty_reason", ""), "review": review})
        source["coverage"] = coverage
        return sid, evidence

    all_evidence = {}
    workers = state["config"].get("workers", 1)
    require(type(workers) is int and workers >= 1, "workers must be a positive integer")
    if workers == 1:
        results = map(one, state["sources"].items())
        for _, rows in results:
            for row in rows:
                all_evidence[row["id"]] = row
    else:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            for _, rows in executor.map(one, state["sources"].items()):
                for row in rows:
                    all_evidence[row["id"]] = row
    return {"evidence": all_evidence}


def validate_candidate(candidate, evidence):
    require(isinstance(candidate.get("summary"), str), "Missing topic summary")
    require(string_list(candidate.get("keywords")) and string_list(candidate.get("gaps")), "Invalid keywords/gaps")
    considered = candidate.get("considered_evidence")
    require(string_list(considered) and len(considered) == len(set(considered)) and set(considered) == set(evidence), "Incomplete evidence consideration")
    require(isinstance(candidate.get("claims"), list) and candidate["claims"], "Nonempty evidence requires claims")
    for claim in candidate["claims"]:
        require(claim.get("kind") in {"source_statement", "inference", "conflict"}, "Invalid claim type")
        require(isinstance(claim.get("text"), str) and claim["text"].strip(), "Empty claim")
        require(isinstance(claim.get("reasoning"), str) and claim["reasoning"].strip(), "Missing derivation")
        require(isinstance(claim.get("conditions"), dict), "Missing claim conditions")
        refs = claim.get("evidence")
        require(string_list(refs) and refs and len(refs) == len(set(refs)) and set(refs) <= set(evidence), "Claim must reference original evidence only")
    for key in ("agreements", "disagreements"):
        indexes = candidate.get(key)
        require(isinstance(indexes, list) and all(type(i) is int for i in indexes)
                and len(indexes) == len(set(indexes)), "Invalid comparison indexes")
        for index in indexes:
            require(type(index) is int and 0 <= index < len(candidate["claims"]), "Bad claim index")
            claim = candidate["claims"][index]
            source_ids = {evidence[e]["source_id"] for e in claim["evidence"]}
            if key == "agreements":
                require(len(source_ids) >= 2, "Cross-document agreement requires multiple sources")
            else:
                require(claim["kind"] == "conflict", "Disagreement must cite conflict claim")
    conflict_indexes = {i for i, c in enumerate(candidate["claims"]) if c["kind"] == "conflict"}
    require(conflict_indexes <= set(candidate["disagreements"]), "Unlisted conflict")


def synthesize(state: BuildState) -> dict:
    pages, rebuilt = {}, []
    stage = Path(state["stage"])
    for topic in state["config"]["topics"]:
        tid = topic["id"]
        rows = {k: v for k, v in state["evidence"].items() if tid in v["topics"]}
        relevant_sources = {e["source_id"]: state["sources"][e["source_id"]] for e in rows.values()}
        inputs = digest({"evidence": rows, "sources": relevant_sources, "topic": topic,
                         "fingerprint": state["fingerprint"], "independence_rule": INDEPENDENCE_RULE_VERSION})
        previous = state["old"].get("pages", {}).get(tid)
        if previous and previous["input_hash"] == inputs:
            pages[tid] = previous
            continue
        if not rows:
            # A removed source must remove its old conclusions, never preserve stale synthesis.
            pages[tid] = {"topic": topic, "input_hash": inputs, "summary": "No usable text evidence is available.",
                          "claims": [], "keywords": [], "agreements": [], "disagreements": [],
                          "gaps": ["No evidence is available; add or reclassify sources."], "considered_evidence": [],
                          "review": {"claims": [], "coverage_ok": True, "issues": []}}
            rebuilt.append(tid)
            continue
        payload = {"topic": topic, "evidence": rows, "sources": relevant_sources}
        request = payload
        for attempt in range(3):
            candidate = state["model"].complete("synthesize", request)
            agreements = candidate.get("agreements")
            if isinstance(agreements, list) and isinstance(candidate.get("claims"), list):
                valid_agreements = []
                for index in agreements:
                    if (type(index) is int and 0 <= index < len(candidate["claims"])
                            and isinstance(candidate["claims"][index], dict)
                            and isinstance(candidate["claims"][index].get("evidence"), list)
                            and len({rows[e]["source_id"] for e in candidate["claims"][index]["evidence"]
                                     if isinstance(e, str) and e in rows}) >= 2):
                        valid_agreements.append(index)
                removed_agreements = [i for i in agreements if i not in valid_agreements]
                candidate["agreements"] = valid_agreements
            else:
                removed_agreements = []
            if removed_agreements:
                atomic_json(stage / "agreement-corrections" / f"{tid}-{attempt}.json",
                            {"removed_entries": removed_agreements,
                             "reason": "Agreement must index one claim citing at least two distinct source IDs"})
            # Persist failed candidates for inspection without changing the current release.
            atomic_json(stage / f"candidate-{tid}.json", candidate)
            try:
                validate_candidate(candidate, rows)
                break
            except ValueError as exc:
                if attempt == 2:
                    raise
                atomic_json(stage / "candidate-attempts" / f"{tid}-{attempt}.json", candidate)
                request = {**payload, "previous_validation_error": str(exc),
                           "correction": "Return a complete corrected candidate. Agreements need evidence from at least two distinct source_id values, not just two evidence IDs; never infer experimental independence from chapter count."}
        expected_indexes = list(range(len(candidate["claims"])))
        review_payload = {**payload, "candidate": candidate, "expected_indexes": expected_indexes}
        for attempt in range(3):
            review = state["model"].complete("review", review_payload)
            checks = review.get("claims")
            indexes = ([c.get("index") for c in checks] if isinstance(checks, list)
                       and all(isinstance(c, dict) for c in checks) else [])
            if all(type(i) is int for i in indexes) and sorted(indexes) == expected_indexes:
                break
            atomic_json(stage / "review-attempts" / f"{tid}-{attempt}.json", review)
            review_payload = {**review_payload, "invalid_review_indexes": indexes}
        atomic_json(stage / f"review-{tid}.json", review)
        require(review.get("coverage_ok") is True and review.get("issues") == [], "Topic review failed")
        checks = review.get("claims")
        require(isinstance(checks, list) and len(checks) == len(candidate["claims"]), "Review did not cover every claim")
        indexes = [c.get("index") for c in checks]
        require(all(type(i) is int for i in indexes) and sorted(indexes) == list(range(len(candidate["claims"]))), "Duplicate/missing claim review")
        require(all(c.get("supported") is True and isinstance(c.get("reason"), str) and c["reason"].strip() for c in checks), "Unsupported claim")
        for claim in candidate["claims"]:
            source_ids = sorted({rows[e]["source_id"] for e in claim["evidence"]})
            groups = [relevant_sources[s]["origin_group"] for s in source_ids]
            claim["support_sources"] = source_ids
            claim["independence"] = (
                "single_source" if len(source_ids) == 1 else
                "unknown" if any(not g for g in groups) else
                "shared_origin" if len(set(groups)) < len(groups) else
                "distinct_declared_origins"
            )
            claim["id"] = "C" + digest([tid, claim])[:24]
        pages[tid] = {**candidate, "topic": topic, "input_hash": inputs, "review": review}
        rebuilt.append(tid)
    return {"pages": pages, "rebuilt_topics": rebuilt}


def render(state: BuildState) -> None:
    stage = Path(state["stage"])
    wiki = stage / "wiki"
    wiki.mkdir()
    groups = {}
    for tid, page in state["pages"].items():
        topic = page["topic"]
        group = topic["group"]
        groups.setdefault(group, []).append(tid)
        directory = wiki / group
        directory.mkdir(exist_ok=True)
        lines = [f"# {topic['title']}", "", page["summary"], "",
                 "Keywords: " + ", ".join(topic["keywords"] + topic["aliases"] + page["keywords"]), "",
                 "Text mode: images, chart layout, and formula fidelity still need visual review.", ""]
        for index, claim in enumerate(page["claims"]):
            lines += [f"## {claim['id']} — {claim['kind']}", "", claim["text"], "",
                      "Conditions: " + json.dumps(claim["conditions"], ensure_ascii=False), "",
                      "Reasoning: " + claim["reasoning"], "",
                      "Source independence: " + claim["independence"] + " (different declared sources do not guarantee independent experiments)", "",
                      "Original evidence: " + ", ".join(f"[{eid}](../../evidence/{eid}.md)" for eid in claim["evidence"]), ""]
        for label, key in (("Shared support", "agreements"), ("Disagreements", "disagreements")):
            lines += ["## " + label, ""]
            lines += [f"- {page['claims'][i]['id']}" for i in page[key]] or ["No reportable items in these materials."]
        lines += ["", "## Knowledge gaps", ""] + ["- " + x for x in page["gaps"]]
        # Rebuildable co-occurrence links: navigation only, never evidence edges.
        source_ids = {state["evidence"][e]["source_id"] for e in page["considered_evidence"]}
        related = [p for key, p in state["pages"].items() if key != tid and
                   source_ids & {state["evidence"][e]["source_id"] for e in p["considered_evidence"]}]
        lines += ["", "## Related topics (navigation)", ""] + [f"- [{p['topic']['title']}](../{p['topic']['group']}/{p['topic']['id']}.md)" for p in related]
        (directory / f"{tid}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    top = ["# ILT knowledge index", "", "This release compiles text evidence only. See the [coverage report](../coverage.md) for visual gaps.", ""]
    for group, tids in groups.items():
        entries = [f"# {group}", ""]
        for tid in tids:
            page = state["pages"][tid]
            entries += [f"- [{page['topic']['title']}]({tid}.md): {page['summary']}",
                        "  Keywords/aliases: " + ", ".join(page["topic"]["keywords"] + page["topic"]["aliases"] + page["keywords"])]
        (wiki / group / "index.md").write_text("\n".join(entries) + "\n", encoding="utf-8")
        top.append(f"- [{group}]({group}/index.md): " + ", ".join(state["pages"][t]["topic"]["title"] for t in tids))
    (wiki / "index.md").write_text("\n".join(top) + "\n", encoding="utf-8")
    (stage / "evidence").mkdir()
    for eid, evidence in state["evidence"].items():
        source = state["sources"][evidence["source_id"]]
        original_link = "../../../objects/" + source["sha256"]
        content = (f"# {eid}\n\nSource: {source['id']} / {source['path']}\n\n"
                   f"SHA256: {source['sha256']}\n\n[Original snapshot]({original_link})\n\n"
                   f"Section: {evidence['section']}; Character offsets: {evidence['start']}:{evidence['end']}; "
                   f"Line: {evidence['line']}; PDF page: {evidence['page']}\n\n"
                   "## Original excerpt\n\n" + evidence["quote"] + "\n")
        (stage / "evidence" / f"{eid}.md").write_text(content, encoding="utf-8")
    coverage = ["# Text coverage and visual gaps", "", "All claims still require ILT expert or empirical validation; automated review cannot establish scientific truth.", ""]
    for source in state["sources"].values():
        coverage += [f"## {source['path']} ({source['id']})", "",
                     f"Text spans processed: {len(source['coverage'])}; Items pending visual review: {len(source['visual_pending'])}", ""]
        if "pdf_pages_selected" in source:
            coverage += ["Sampled PDF pages (1-based source pages): " + ", ".join(map(str, source["pdf_pages_selected"])),
                         "Other pages were not used for text extraction, topic synthesis, or question answering.", ""]
        if not source["coverage"]:
            coverage += ["- No text was extracted; do not treat this as a complete reading."]
        coverage += ["- " + json.dumps(p, ensure_ascii=False) for p in source["visual_pending"]]
    (stage / "coverage.md").write_text("\n".join(coverage) + "\n", encoding="utf-8")
    for path in stage.rglob("*.md"):
        path.write_text(GENERATED_MARKER + "\n\n" + path.read_text(encoding="utf-8"), encoding="utf-8")


def validate_release(release: Path, objects: Path) -> list[str]:
    manifest = read_json(release / "manifest.json")
    expected_files = {p.relative_to(release).as_posix() for folder in ("wiki", "evidence")
                      for p in (release / folder).rglob("*.md")} | {"coverage.md"}
    require(set(manifest["artifacts"]) == expected_files, "Missing/unregistered knowledge artifacts")
    for relative, sha in manifest["artifacts"].items():
        path = release / relative
        require(path.resolve().is_relative_to(release.resolve()), "Unsafe artifact path")
        require(digest(path.read_bytes()) == sha, "Generated artifact hash mismatch")
    evidence = manifest["evidence"]
    texts = {}
    for sid, source in manifest["sources"].items():
        require(source["id"] == sid, "Source identity mismatch")
        if "pdf_pages_selected" in source:
            selected = source["pdf_pages_selected"]
            require(isinstance(selected, list) and selected == sorted(set(selected))
                    and selected == [p["page"] for p in source["pages"]], "Invalid PDF page selection")
        for key in ("sha256", "text_hash"):
            require(re.fullmatch(r"[a-f0-9]{64}", source[key]), "Invalid snapshot hash")
            require(digest((objects / source[key]).read_bytes()) == source[key], "Source snapshot hash mismatch")
        text = (objects / source["text_hash"]).read_bytes().decode("utf-8")
        texts[sid] = text
        offset = 0
        for part in source["coverage"]:
            require(part["start"] == offset and part["end"] > offset, "Text coverage gap/overlap")
            require(digest(text[offset:part["end"]]) == part["text_hash"], "Coverage hash mismatch")
            offset = part["end"]
        require(offset == len(text), "Unprocessed text range")
    for eid, row in evidence.items():
        require(row["id"] == eid and row["source_id"] in manifest["sources"], "Invalid evidence source")
        source = manifest["sources"][row["source_id"]]
        require(source["status"] == "active", "Withdrawn source used as evidence")
        text = texts[row["source_id"]]
        require(row["source_hash"] == source["sha256"], "Stale evidence")
        require(0 <= row["start"] < row["end"] <= len(text) and text[row["start"]:row["end"]] == row["quote"], "Evidence locator mismatch")
        require(eid == "E" + digest([row["source_id"], source["sha256"], row["start"],
                                     row["quote"], row["statement"]])[:24], "Evidence identity mismatch")
    for tid, page in manifest["pages"].items():
        expected = {eid for eid, row in evidence.items() if tid in row["topics"]}
        require(set(page["considered_evidence"]) == expected, "Topic membership/dependency mismatch")
        rows = {eid: evidence[eid] for eid in page["considered_evidence"]}
        relevant_sources = {row["source_id"]: manifest["sources"][row["source_id"]] for row in rows.values()}
        expected_input = digest({"evidence": rows, "sources": relevant_sources, "topic": page["topic"],
                                 "fingerprint": manifest["fingerprint"],
                                 "independence_rule": manifest.get("independence_rule_version", "single-source-v2")})
        require(page["input_hash"] == expected_input, "Topic input fingerprint mismatch")
        if rows:
            validate_candidate(page, rows)
        else:
            require(not page["claims"], "Claims without evidence")
        for claim in page["claims"]:
            payload = {key: value for key, value in claim.items() if key != "id"}
            require(claim["id"] == "C" + digest([tid, payload])[:24], "Claim identity mismatch")
            source_ids = sorted({rows[eid]["source_id"] for eid in claim["evidence"]})
            require(claim["support_sources"] == source_ids, "Claim source identity mismatch")
            groups = [relevant_sources[sid]["origin_group"] for sid in source_ids]
            independence = ("single_source" if len(source_ids) == 1 else
                            "unknown" if any(not group for group in groups) else
                            "shared_origin" if len(set(groups)) < len(groups) else
                            "distinct_declared_origins")
            require(claim["independence"] == independence, "Claim independence mismatch")
        review = page["review"]
        require(review.get("coverage_ok") is True and review.get("issues") == [], "Missing passing semantic review")
        checks = review.get("claims", [])
        require(len(checks) == len(page["claims"]) and
                sorted(c["index"] for c in checks) == list(range(len(page["claims"]))) and
                all(c.get("supported") is True for c in checks), "Incomplete claim review")
    for path in (release / "wiki").rglob("*.md"):
        for link in re.findall(r"\]\(([^\s)]+)\)", path.read_text(encoding="utf-8")):
            if "://" in link or link.startswith("#"):
                continue
            target = (path.parent / link.split("#")[0]).resolve()
            # Original source text may contain links; inspect only compiler-owned navigation.
            if path.parent.name != "evidence":
                require(target.is_relative_to(release.resolve()) and target.is_file(), f"Broken generated link: {link}")
    return [f"{s['path']}: visuals pending" for s in manifest["sources"].values() if s["visual_pending"]]


def prepare(state: BuildState) -> dict:
    render(state)
    stage = Path(state["stage"])
    manifest = {"schema": 1, "fingerprint": state["fingerprint"],
                "independence_rule_version": INDEPENDENCE_RULE_VERSION, "sources": state["sources"],
                "evidence": state["evidence"], "pages": state["pages"],
                "rebuilt_topics": state["rebuilt_topics"], "scope": "text-only"}
    artifacts = [p for folder in ("wiki", "evidence") for p in (stage / folder).rglob("*.md")]
    artifacts.append(stage / "coverage.md")
    manifest["artifacts"] = {p.relative_to(stage).as_posix(): digest(p.read_bytes()) for p in artifacts}
    atomic_json(stage / "manifest.json", manifest)
    validate_release(stage, Path(state["root"]) / ".iltkb" / "objects")
    return {}


def publish(state: BuildState) -> dict:
    root, stage = Path(state["root"]), Path(state["stage"])
    releases = root / ".iltkb" / "releases"
    releases.mkdir(exist_ok=True)
    destination = releases / stage.name
    stage.rename(destination)
    manifest_hash = digest((destination / "manifest.json").read_bytes())
    if state["config"].get("publication_policy", "review_required") == "review_required":
        current = current_release(root)
        atomic_json(root / ".iltkb" / "pending.json",
                    {"release": destination.name, "base_release": current.name if current else None,
                     "manifest_sha256": manifest_hash})
    else:
        atomic_json(root / ".iltkb" / "current.json", {"release": destination.name,
                                                       "manifest_sha256": manifest_hash})
    return {"stage": str(destination)}


def build(root: Path, model) -> dict:
    from langgraph.graph import StateGraph, START, END
    from langsmith import tracing_context
    root = root.resolve()
    config = read_json(root / "iltkb.json")
    with writer_lock(root / ".iltkb"), tracing_context(enabled=False):
        if (root / ".iltkb" / "pending.json").exists():
            raise ValueError("A candidate release awaits review; publish or discard it before rebuilding")
        # Validate the last published version before reusing any of its cached claims.
        previous_release = current_release(root)
        if previous_release:
            validate_release(previous_release, root / ".iltkb" / "objects")
        state = {"root": str(root), "config": config, "model": model}
        workflow = StateGraph(BuildState)
        steps = [("scan", scan), ("extract", extract), ("synthesize", synthesize),
                 ("validate", prepare), ("publish", publish)]
        previous = START
        for name, function in steps:
            workflow.add_node(name, function)
            workflow.add_edge(previous, name)
            previous = name
        workflow.add_edge(previous, END)
        # No Store, checkpointer, server or tracing service is needed.
        result = workflow.compile().invoke(state)
        return {"release": Path(result["stage"]).name,
                "status": "pending_review" if config.get("publication_policy", "review_required") == "review_required" else "published",
                "sources": len(result["sources"]),
                "evidence": len(result["evidence"]), "rebuilt_topics": result["rebuilt_topics"]}
