"""Bounded, version-pinned research workflow. Nothing here publishes knowledge."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import time
from typing import TypedDict
from uuid import uuid4

from langgraph.graph import END, START, StateGraph

from .model import RESEARCH_PROMPT_HASH
from .retrieval import Reader, tokens


class ResearchConflict(ValueError):
    """The task changed after the client loaded it."""


class ResearchState(TypedDict, total=False):
    question: str
    topic_id: str | None
    context: dict
    candidate: dict
    additional_context: dict
    critique: dict
    task: dict
    assessment: dict


def _sentence(value, name: str, limit: int = 1200) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"Invalid {name}")
    return value.strip()


def _ids(value, allowed: set[str], name: str) -> list[str]:
    if not isinstance(value, list) or len(value) > 8 or any(not isinstance(item, str) or item not in allowed for item in value):
        raise ValueError(f"Invalid {name}: unknown or excessive references")
    return list(dict.fromkeys(value))


def _plan_candidate(candidate: dict, allowed: set[str]) -> dict:
    if not isinstance(candidate, dict):
        raise ValueError("Invalid research proposal")
    requests = candidate.get("requests")
    if not isinstance(requests, list) or not 1 <= len(requests) <= 3:
        raise ValueError("Research proposal needs one to three concrete requests")
    clean = {key: _sentence(candidate.get(key), key) for key in ("hypothesis", "uncertainty")}
    clean["support_evidence"] = _ids(candidate.get("support_evidence"), allowed, "support evidence")
    clean["counter_evidence"] = _ids(candidate.get("counter_evidence"), allowed, "counter evidence")
    clean["requests"] = []
    for request in requests:
        if not isinstance(request, dict) or request.get("kind") not in ("source", "validation"):
            raise ValueError("Invalid research request kind")
        clean["requests"].append({"kind": request["kind"], **{
            key: _sentence(request.get(key), key) for key in ("question", "why", "acceptance")}})
    if not allowed and not any(request["kind"] == "source" for request in clean["requests"]):
        raise ValueError("Research without evidence must request an original source")
    return clean


def _assessment(candidate: dict, evidence: set[str], materials: set[str]) -> dict:
    if not isinstance(candidate, dict) or candidate.get("assessment") not in (
        "insufficient", "potentially_supported", "potentially_refuted"
    ):
        raise ValueError("Invalid research assessment")
    used_materials = _ids(candidate.get("materials"), materials, "assessment materials")
    if not used_materials:
        raise ValueError("Assessment must identify at least one submitted material")
    return {"assessment": candidate["assessment"],
            "reason": _sentence(candidate.get("reason"), "reason", 3000),
            "evidence": _ids(candidate.get("evidence"), evidence, "assessment evidence"),
            "materials": used_materials,
            "remaining_gap": _sentence(candidate.get("remaining_gap"), "remaining gap", 2000)}


def _critique(candidate: dict, allowed: set[str]) -> dict:
    if not isinstance(candidate, dict) or candidate.get("verdict") not in (
        "insufficient", "possible_counterexample", "no_counterexample_found"
    ):
        raise ValueError("Invalid research critique")
    assumptions = candidate.get("assumptions")
    if not isinstance(assumptions, list) or len(assumptions) > 5:
        raise ValueError("Invalid research assumptions")
    return {"verdict": candidate["verdict"],
            "challenge": _sentence(candidate.get("challenge"), "challenge", 2500),
            "counter_evidence": _ids(candidate.get("counter_evidence"), allowed, "critique evidence"),
            "assumptions": [_sentence(item, "assumption", 1000) for item in assumptions],
            "next_check": _sentence(candidate.get("next_check"), "next check", 1500)}


def _context(reader: Reader, question: str, topic_id: str | None) -> dict:
    if topic_id is not None:
        page = reader.manifest["pages"].get(topic_id)
        if page is None:
            raise ValueError("Unknown topic ID")
        pages = {topic_id: page}
        refs = set(page["considered_evidence"])
    else:
        matched = reader.context(question, limit=3)
        pages = matched["topics"]
        refs = {eid for page in pages.values() for eid in page["considered_evidence"]}
    evidence = {}
    skipped = 0
    query_terms = tokens(question)
    ranked = sorted(refs, key=lambda eid: (
        -len(query_terms & tokens(reader.manifest["evidence"][eid]["statement"] + " " +
                                    reader.manifest["evidence"][eid]["quote"])), eid))
    diversified, seen_sources = [], set()
    for eid in ranked:
        sid = reader.manifest["evidence"][eid]["source_id"]
        if sid not in seen_sources:
            diversified.append(eid)
            seen_sources.add(sid)
    selected_order = diversified + [eid for eid in ranked if eid not in diversified]
    for eid in selected_order:
        row = reader.manifest["evidence"][eid]
        quote = row["quote"]
        if len(evidence) >= 8 or len(quote) > 2400:
            skipped += 1
            continue
        source = reader.manifest["sources"][row["source_id"]]
        evidence[eid] = {"quote": quote, "statement": row["statement"],
                         "source_id": row["source_id"], "source_title": source["title"],
                         "section": row.get("section"), "page": row.get("page")}
    topics = {tid: {"title": page["topic"]["title"], "gaps": page.get("gaps", [])[:6]}
              for tid, page in pages.items()}
    return {"release": reader.release.name, "question": question, "topic_id": topic_id,
            "topics": topics, "evidence": evidence, "omitted_evidence_count": skipped,
            "scope": "text-only; all generated hypotheses remain unverified"}


def _additional_context(reader: Reader, context: dict, hypothesis: str) -> dict:
    refs = {eid for tid in context["topics"] for eid in reader.manifest["pages"][tid]["considered_evidence"]}
    remaining = refs - set(context["evidence"])
    query_terms = tokens(context["question"] + " " + hypothesis)
    ranked = sorted(remaining, key=lambda eid: (
        -len(query_terms & tokens(reader.manifest["evidence"][eid]["statement"] + " " +
                                    reader.manifest["evidence"][eid]["quote"])), eid))
    extra = {}
    for eid in ranked:
        row = reader.manifest["evidence"][eid]
        if len(extra) >= 8:
            break
        if len(row["quote"]) > 2400:
            continue
        source = reader.manifest["sources"][row["source_id"]]
        extra[eid] = {"quote": row["quote"], "statement": row["statement"],
                      "source_id": row["source_id"], "source_title": source["title"],
                      "section": row.get("section"), "page": row.get("page")}
    return {"evidence": extra, "omitted_evidence_count": len(remaining) - len(extra)}


class ResearchStore:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.path = self.root / ".iltkb" / "research.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS tasks (
                  id TEXT PRIMARY KEY, release TEXT NOT NULL, question TEXT NOT NULL,
                  topic_id TEXT, context TEXT NOT NULL, candidate TEXT NOT NULL,
                  assessment TEXT, status TEXT NOT NULL, version INTEGER NOT NULL,
                  model_fingerprint TEXT NOT NULL, created_by TEXT NOT NULL,
                  updated_by TEXT NOT NULL, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS materials (
                  id TEXT PRIMARY KEY, task_id TEXT NOT NULL, filename TEXT NOT NULL,
                  sha256 TEXT NOT NULL, bytes INTEGER NOT NULL, submitted_by TEXT NOT NULL,
                  created_at INTEGER NOT NULL, FOREIGN KEY(task_id) REFERENCES tasks(id));
                CREATE TABLE IF NOT EXISTS events (
                  id INTEGER PRIMARY KEY, task_id TEXT NOT NULL, actor TEXT NOT NULL,
                  action TEXT NOT NULL, detail TEXT NOT NULL, created_at INTEGER NOT NULL);
                CREATE INDEX IF NOT EXISTS idx_research_status ON tasks(status,updated_at);
                CREATE INDEX IF NOT EXISTS idx_research_materials ON materials(task_id);
            """)
            db.execute("BEGIN IMMEDIATE")
            for row in db.execute("SELECT id,candidate FROM tasks WHERE status='awaiting_material'").fetchall():
                if "critique" not in json.loads(row["candidate"]):
                    db.execute("UPDATE tasks SET status='awaiting_critique',version=version+1,updated_at=? WHERE id=?",
                               (int(time.time()), row["id"]))
                    self._event(db, row["id"], "system", "legacy_critique_required", {})
        self.path.chmod(0o600)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=10000")
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def _event(self, db, task_id: str, actor: str, action: str, detail: dict) -> None:
        db.execute("INSERT INTO events(task_id,actor,action,detail,created_at) VALUES(?,?,?,?,?)",
                   (task_id, actor, action, json.dumps(detail, ensure_ascii=False), int(time.time())))

    def existing(self, release: str, question: str, topic_id: str | None) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT id FROM tasks WHERE release=? AND question=? AND topic_id IS ? "
                             "AND status<>'rejected' ORDER BY created_at DESC LIMIT 1",
                             (release, question, topic_id)).fetchone()
        return self.get(row["id"]) if row else None

    def create(self, context: dict, candidate: dict, model_fingerprint: dict, actor: str,
               status: str = "awaiting_critique") -> dict:
        if status != "awaiting_critique":
            raise ValueError("Invalid initial research status")
        task_id, now = uuid4().hex, int(time.time())
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT id FROM tasks WHERE release=? AND question=? AND topic_id IS ? "
                                  "AND status<>'rejected' ORDER BY created_at DESC LIMIT 1",
                                  (context["release"], context["question"], context["topic_id"])).fetchone()
            if existing:
                task_id = existing["id"]
            else:
                db.execute("INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (task_id, context["release"], context["question"], context["topic_id"],
                            json.dumps(context, ensure_ascii=False), json.dumps(candidate, ensure_ascii=False),
                            None, status, 1, json.dumps(model_fingerprint, ensure_ascii=False),
                            actor, actor, now, now))
                self._event(db, task_id, actor, "proposed", {"requests": len(candidate["requests"])})
        return self.get(task_id)

    def finish_critique(self, task_id: str, context: dict, candidate: dict,
                        actor: str, expected_version: int, model_fingerprint: dict) -> dict:
        now = int(time.time())
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT version,status,release FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not row:
                raise ValueError("Unknown research task")
            if row["version"] != expected_version:
                raise ResearchConflict("Research task changed; reload before critique")
            if row["status"] != "awaiting_critique" or row["release"] != context["release"]:
                raise ValueError("Research task is not awaiting critique")
            db.execute("UPDATE tasks SET context=?,candidate=?,status='awaiting_material',version=version+1,"
                       "updated_by=?,updated_at=? WHERE id=?",
                       (json.dumps(context, ensure_ascii=False), json.dumps(candidate, ensure_ascii=False),
                        actor, now, task_id))
            self._event(db, task_id, actor, "critiqued", {"verdict": candidate["critique"]["verdict"],
                                                          "model_fingerprint": model_fingerprint,
                                                          "research_prompt": RESEARCH_PROMPT_HASH})
        return self.get(task_id)

    def get(self, task_id: str) -> dict:
        if not isinstance(task_id, str) or not re.fullmatch(r"[0-9a-f]{32}", task_id):
            raise ValueError("Invalid research task ID")
        with self.connect() as db:
            row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not row:
                raise ValueError("Unknown research task")
            item = dict(row)
            item["context"] = json.loads(item["context"])
            item["candidate"] = json.loads(item["candidate"])
            item["assessment"] = json.loads(item["assessment"]) if item["assessment"] else None
            item["model_fingerprint"] = json.loads(item["model_fingerprint"])
            item["materials"] = [dict(r) for r in db.execute(
                "SELECT * FROM materials WHERE task_id=? ORDER BY created_at,id", (task_id,))]
            item["events"] = [dict(r) for r in db.execute(
                "SELECT actor,action,detail,created_at FROM events WHERE task_id=? ORDER BY id", (task_id,))]
            return item

    def list(self, limit: int = 100) -> list[dict]:
        with self.connect() as db:
            ids = [row[0] for row in db.execute(
                "SELECT id FROM tasks ORDER BY updated_at DESC,id DESC LIMIT ?", (min(max(limit, 1), 100),))]
        items = [self.get(task_id) for task_id in ids]
        for item in items:
            item["context"] = {"evidence_ids": list(item["context"]["evidence"]),
                               "proposer_evidence_count": item["context"].get("proposer_evidence_count"),
                               "omitted_evidence_count": item["context"]["omitted_evidence_count"],
                               "scope": item["context"]["scope"]}
        return items

    def submit(self, task_id: str, filename: str, content: str, actor: str, expected_version: int) -> dict:
        if not isinstance(filename, str) or not re.fullmatch(r"[\w][\w .()\-]{0,119}\.(?:md|markdown|txt)", filename, re.I):
            raise ValueError("Supply a simple .md, .markdown or .txt filename")
        if not isinstance(content, str) or not content.strip() or len(content.encode("utf-8")) > 2_000_000:
            raise ValueError("Material must be 1–2 MB of UTF-8 text")
        if type(expected_version) is not int or expected_version < 1:
            raise ValueError("Invalid research task version")
        blob = content.encode("utf-8")
        sha = hashlib.sha256(blob).hexdigest()
        objects = self.root / ".iltkb" / "research_objects"
        objects.mkdir(parents=True, exist_ok=True)
        objects.chmod(0o700)
        target = objects / sha
        material_id, now = uuid4().hex, int(time.time())
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT version,status,release FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not row:
                raise ValueError("Unknown research task")
            if row["version"] != expected_version:
                raise ResearchConflict("Research task changed; reload before submitting")
            if Reader(self.root).release.name != row["release"]:
                raise ResearchConflict("Research task references an old release; replan against the current release")
            if row["status"] not in ("awaiting_material", "awaiting_assessment"):
                raise ValueError("Research task is not accepting material")
            if not target.exists():
                with tempfile.NamedTemporaryFile(dir=objects, prefix=".incoming-", delete=False) as stream:
                    temp_path = Path(stream.name)
                    stream.write(blob)
                    stream.flush()
                    os.fsync(stream.fileno())
                try:
                    temp_path.chmod(0o600)
                    try:
                        os.link(temp_path, target)
                    except FileExistsError:
                        pass
                finally:
                    temp_path.unlink(missing_ok=True)
            if hashlib.sha256(target.read_bytes()).hexdigest() != sha:
                raise ValueError("Stored material hash mismatch")
            db.execute("INSERT INTO materials VALUES(?,?,?,?,?,?,?)",
                       (material_id, task_id, filename, sha, len(blob), actor, now))
            db.execute("UPDATE tasks SET status='awaiting_assessment',version=version+1,updated_by=?,updated_at=? WHERE id=?",
                       (actor, now, task_id))
            self._event(db, task_id, actor, "material_submitted", {"id": material_id, "sha256": sha})
        return self.get(task_id)

    def material(self, task_id: str, material_id: str) -> tuple[str, bytes]:
        if not re.fullmatch(r"[0-9a-f]{32}", material_id or ""):
            raise ValueError("Invalid material ID")
        with self.connect() as db:
            row = db.execute("SELECT filename,sha256 FROM materials WHERE id=? AND task_id=?",
                             (material_id, task_id)).fetchone()
        if not row:
            raise ValueError("Unknown material")
        blob = (self.root / ".iltkb" / "research_objects" / row["sha256"]).read_bytes()
        if hashlib.sha256(blob).hexdigest() != row["sha256"]:
            raise ValueError("Stored material hash mismatch")
        return row["filename"], blob

    def save_assessment(self, task_id: str, result: dict, actor: str, expected_version: int,
                        model_fingerprint: dict) -> dict:
        now = int(time.time())
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT version,status,release FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not row:
                raise ValueError("Unknown research task")
            if row["version"] != expected_version:
                raise ResearchConflict("Research task changed; reload before assessment")
            if row["status"] != "awaiting_assessment":
                raise ValueError("Research task is not ready for assessment")
            db.execute("UPDATE tasks SET assessment=?,status=?,version=version+1,updated_by=?,updated_at=? WHERE id=?",
                       (json.dumps(result, ensure_ascii=False),
                        "awaiting_material" if result["assessment"] == "insufficient" else "awaiting_review",
                        actor, now, task_id))
            self._event(db, task_id, actor, "assessed", {"assessment": result["assessment"],
                                                         "model_fingerprint": model_fingerprint,
                                                         "research_prompt": RESEARCH_PROMPT_HASH})
        return self.get(task_id)

    def decide(self, task_id: str, decision: str, note: str, actor: str, expected_version: int) -> dict:
        if decision not in ("accept_for_followup", "reject"):
            raise ValueError("Invalid research decision")
        note = _sentence(note, "review note", 4000)
        if type(expected_version) is not int or expected_version < 1:
            raise ValueError("Invalid research task version")
        now = int(time.time())
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT version,status,release FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not row:
                raise ValueError("Unknown research task")
            if row["version"] != expected_version:
                raise ResearchConflict("Research task changed; reload before decision")
            if Reader(self.root).release.name != row["release"]:
                raise ResearchConflict("Research task references an old release; replan against the current release")
            if row["status"] != "awaiting_review":
                raise ValueError("Only assessed tasks can be decided")
            db.execute("UPDATE tasks SET status=?,version=version+1,updated_by=?,updated_at=? WHERE id=?",
                       ("followup_accepted" if decision == "accept_for_followup" else "rejected", actor, now, task_id))
            self._event(db, task_id, actor, decision, {"note": note})
        return self.get(task_id)


def suggest_gaps(root: Path, limit: int = 12) -> list[dict]:
    """Surface existing model-reported gaps without another model call."""
    reader = Reader(root)
    with ResearchStore(root).connect() as db:
        existing = {(row["question"], row["topic_id"]) for row in db.execute(
            "SELECT question,topic_id FROM tasks WHERE release=? AND status<>'rejected'",
            (reader.release.name,))}
    suggestions = []
    for topic_id, page in sorted(reader.manifest["pages"].items()):
        for gap in page.get("gaps", [])[:4]:
            if not isinstance(gap, str) or not gap.strip() or len(gap) > 1000:
                continue
            question = gap.strip()
            if (question, topic_id) not in existing:
                suggestions.append({"topic_id": topic_id, "topic": page["topic"]["title"],
                                    "question": question, "origin": "model_reported_gap"})
    return suggestions[:max(0, min(limit, 30))]


def plan(root: Path, question: str, model, *, topic_id: str | None = None, actor: str = "local-cli") -> dict:
    question = _sentence(question, "question", 1000)
    reader = Reader(root)
    store = ResearchStore(root)
    existing = store.existing(reader.release.name, question, topic_id)
    if existing:
        return continue_plan(root, existing["id"], model, actor=actor) if existing["status"] == "awaiting_critique" else existing

    def retrieve(state: ResearchState) -> dict:
        return {"context": _context(reader, state["question"], state.get("topic_id"))}

    def propose(state: ResearchState) -> dict:
        return {"candidate": model.complete("research_plan", state["context"])}

    def validate_proposal(state: ResearchState) -> dict:
        return {"candidate": _plan_candidate(state["candidate"], set(state["context"]["evidence"]))}

    def save_draft(state: ResearchState) -> dict:
        fresh = Reader(root)
        if fresh.release.name != state["context"]["release"]:
            raise ResearchConflict("Release changed during research plan")
        return {"task": store.create(state["context"], state["candidate"],
                                     {"model": model.fingerprint, "research_prompt": RESEARCH_PROMPT_HASH}, actor)}

    graph = StateGraph(ResearchState)
    graph.add_node("retrieve", retrieve)
    graph.add_node("propose", propose)
    graph.add_node("validate_proposal", validate_proposal)
    graph.add_node("save_draft", save_draft)
    graph.add_edge(START, "retrieve")
    graph.add_edge("retrieve", "propose")
    graph.add_edge("propose", "validate_proposal")
    graph.add_edge("validate_proposal", "save_draft")
    graph.add_edge("save_draft", END)
    draft = graph.compile().invoke({"question": question, "topic_id": topic_id})["task"]
    return continue_plan(root, draft["id"], model, actor=actor) if draft["status"] == "awaiting_critique" else draft


def continue_plan(root: Path, task_id: str, model, *, actor: str = "local-cli") -> dict:
    """Retry only the critique stage after a provider failure or process restart."""
    store = ResearchStore(root)
    reader = Reader(root)

    def load(state: ResearchState) -> dict:
        task = store.get(task_id)
        if task["status"] != "awaiting_critique":
            raise ValueError("Research task is not awaiting critique")
        if task["release"] != reader.release.name:
            raise ResearchConflict("Research task references an old release; replan against the current release")
        return {"task": task, "context": task["context"], "candidate": task["candidate"]}

    def countersearch(state: ResearchState) -> dict:
        return {"additional_context": _additional_context(reader, state["context"],
                                                          state["candidate"]["hypothesis"])}

    def critique(state: ResearchState) -> dict:
        return {"critique": model.complete("research_critique", {
            "question": state["task"]["question"], "candidate": state["candidate"],
            "proposer_evidence": state["context"]["evidence"],
            "additional_evidence": state["additional_context"]["evidence"],
            "omitted_evidence_count": state["additional_context"]["omitted_evidence_count"],
            "scope": "independent tentative challenge; no scientific certification"})}

    def validate_and_save(state: ResearchState) -> dict:
        context = {**state["context"],
                   "evidence": {**state["context"]["evidence"], **state["additional_context"]["evidence"]},
                   "omitted_evidence_count": state["additional_context"]["omitted_evidence_count"],
                   "proposer_evidence_count": len(state["context"]["evidence"])}
        candidate = {**state["candidate"], "critique": _critique(state["critique"], set(context["evidence"]))}
        fresh = Reader(root)
        if fresh.release.name != context["release"]:
            raise ResearchConflict("Release changed during research critique")
        return {"task": store.finish_critique(task_id, context, candidate, actor,
                                               state["task"]["version"], model.fingerprint)}

    graph = StateGraph(ResearchState)
    graph.add_node("load", load)
    graph.add_node("countersearch", countersearch)
    graph.add_node("critique", critique)
    graph.add_node("validate_and_save", validate_and_save)
    graph.add_edge(START, "load")
    graph.add_edge("load", "countersearch")
    graph.add_edge("countersearch", "critique")
    graph.add_edge("critique", "validate_and_save")
    graph.add_edge("validate_and_save", END)
    return graph.compile().invoke({})["task"]


def assess(root: Path, task_id: str, model, *, actor: str = "local-cli") -> dict:
    store = ResearchStore(root)

    def load(state: ResearchState) -> dict:
        task = store.get(task_id)
        if task["status"] != "awaiting_assessment" or not task["materials"]:
            raise ValueError("Research task has no new material to assess")
        reader = Reader(root)
        if reader.release.name != task["release"]:
            raise ResearchConflict("Research task references an old release; replan against the current release")
        return {"task": task}

    def infer(state: ResearchState) -> dict:
        task = state["task"]
        materials = []
        for row in task["materials"]:
            filename, blob = store.material(task_id, row["id"])
            if len(blob) > 16_000:
                # No silent truncation: only submit a bounded excerpt as a new material.
                raise ValueError("Material exceeds 16 KB model assessment limit; submit a focused excerpt")
            materials.append({"sha256": row["sha256"], "filename": filename,
                              "text": blob.decode("utf-8")})
        if len(materials) > 8:
            raise ValueError("Too many research materials for one assessment")
        payload = {"question": task["question"], "candidate": task["candidate"],
                   "original_evidence": task["context"]["evidence"], "materials": materials,
                   "scope": "tentative assessment; no scientific certification"}
        return {"assessment": model.complete("research_assess", payload)}

    def validate_and_save(state: ResearchState) -> dict:
        task = state["task"]
        clean = _assessment(state["assessment"], set(task["context"]["evidence"]),
                            {row["sha256"] for row in task["materials"]})
        fresh = Reader(root)
        if fresh.release.name != task["release"]:
            raise ResearchConflict("Release changed during research assessment")
        return {"task": store.save_assessment(task_id, clean, actor, task["version"], model.fingerprint)}

    graph = StateGraph(ResearchState)
    graph.add_node("load", load)
    graph.add_node("infer", infer)
    graph.add_node("validate_and_save", validate_and_save)
    graph.add_edge(START, "load")
    graph.add_edge("load", "infer")
    graph.add_edge("infer", "validate_and_save")
    graph.add_edge("validate_and_save", END)
    return graph.compile().invoke({})["task"]
