"""Review-gated promotion of immutable build candidates."""
from __future__ import annotations

from pathlib import Path
import json
import re
import time

from .management import ReviewStore
from .storage import atomic_json, current_release, digest, read_json, writer_lock


def pending_candidate(root: Path) -> dict | None:
    root = root.resolve()
    pointer = root / ".iltkb" / "pending.json"
    if not pointer.exists():
        return None
    data = read_json(pointer)
    if not isinstance(data, dict) or not isinstance(data.get("release"), str) or not re.fullmatch(r"[0-9a-f]{32}", data["release"]):
        raise ValueError("Invalid pending release pointer")
    base = data.get("base_release")
    if base is not None and (not isinstance(base, str) or not re.fullmatch(r"[0-9a-f]{32}", base)):
        raise ValueError("Invalid pending base release")
    path = root / ".iltkb" / "releases" / data["release"]
    if not path.is_dir() or path.is_symlink():
        raise ValueError("Pending release is missing or unsafe")
    manifest_hash = data.get("manifest_sha256")
    if not isinstance(manifest_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", manifest_hash):
        raise ValueError("Invalid pending manifest hash")
    if digest((path / "manifest.json").read_bytes()) != manifest_hash:
        raise ValueError("Pending manifest hash mismatch")
    if base is not None:
        base_path = root / ".iltkb" / "releases" / base
        if not base_path.is_dir() or base_path.is_symlink():
            raise ValueError("Pending base release is missing or unsafe")
    return {**data, "path": path.resolve()}


def candidate_report(root: Path, *, review_store: ReviewStore | None = None,
                     review_db=None) -> dict | None:
    root = root.resolve()
    pending = pending_candidate(root)
    if pending is None:
        return None
    from .engine import validate_release
    validate_release(pending["path"], root / ".iltkb" / "objects")
    manifest = read_json(pending["path"] / "manifest.json")
    base_path = root / ".iltkb" / "releases" / pending["base_release"] if pending["base_release"] else None
    if base_path:
        validate_release(base_path, root / ".iltkb" / "objects")
    base = read_json(base_path / "manifest.json") if base_path else {"pages": {}, "sources": {}}
    old_sources, new_sources = base["sources"], manifest["sources"]
    old_pages, new_pages = base["pages"], manifest["pages"]
    changes = {
        "sources_added": [new_sources[sid]["path"] for sid in sorted(new_sources.keys() - old_sources.keys())],
        "sources_removed": [old_sources[sid]["path"] for sid in sorted(old_sources.keys() - new_sources.keys())],
        "sources_changed": [new_sources[sid]["path"] for sid in sorted(new_sources.keys() & old_sources.keys())
                            if digest(new_sources[sid]) != digest(old_sources[sid])],
        "topics_added": [new_pages[tid]["topic"]["title"] for tid in sorted(new_pages.keys() - old_pages.keys())],
        "topics_removed": [old_pages[tid]["topic"]["title"] for tid in sorted(old_pages.keys() - new_pages.keys())],
        "topics_changed": [new_pages[tid]["topic"]["title"] for tid in sorted(new_pages.keys() & old_pages.keys())
                           if new_pages[tid]["input_hash"] != old_pages[tid]["input_hash"]],
    }
    store = review_store or ReviewStore(root)
    reviews = store.reviews(pending["release"], review_db)
    prior = store.reviews(pending["base_release"], review_db) if pending["base_release"] else {}
    items = []
    for topic_id, page in manifest["pages"].items():
        previous = base["pages"].get(topic_id)
        unchanged_topic = bool(previous and previous["input_hash"] == page["input_hash"])
        disagreements = set(page.get("disagreements", []))
        for index, claim in enumerate(page["claims"]):
            fingerprint = store.fingerprint(claim)
            review = reviews.get(claim["id"])
            carried = (prior.get(claim["id"]) if unchanged_topic else None)
            carried_ok = bool(carried and carried["decision"] == "approved" and
                              not carried["flags"] and carried["fingerprint"] == fingerprint)
            approved = bool(review and review["decision"] == "approved" and
                            not review["flags"] and review["fingerprint"] == fingerprint)
            auto_flags = (["conflict"] if claim["kind"] == "conflict" else []) + (
                ["inconsistent"] if index in disagreements else [])
            if review and review["fingerprint"] != fingerprint:
                auto_flags.append("stale")
            carry_allowed = carried_ok and not auto_flags
            evidence_details = []
            for evidence_id in claim["evidence"]:
                evidence = manifest["evidence"][evidence_id]
                source = manifest["sources"][evidence["source_id"]]
                evidence_details.append({"id": evidence_id, "quote": evidence["quote"],
                                         "statement": evidence["statement"], "source": source["title"],
                                         "source_path": source["path"], "page": evidence["page"],
                                         "section": evidence["section"]})
            items.append({"id": claim["id"], "topic_id": topic_id, "topic": page["topic"]["title"],
                          "text": claim["text"], "kind": claim["kind"], "evidence": claim["evidence"],
                          "reasoning": claim["reasoning"], "conditions": claim["conditions"],
                          "evidence_details": evidence_details,
                          "auto_flags": auto_flags, "required": not carry_allowed or bool(review),
                          "carried_approval": carry_allowed and review is None,
                          "approved": (approved if review is not None else carry_allowed) and not auto_flags,
                          "review": ({key: review[key] for key in ("decision", "flags", "note", "username", "updated_at", "version")}
                                     if review else None)})
    needed = [item for item in items if not item["approved"]]
    return {"release": pending["release"], "base_release": pending["base_release"], "changes": changes,
            "items": items, "ready": not needed,
            "stats": {"total": len(items), "needs_review": len(needed),
                      "carried": sum(item["carried_approval"] for item in items),
                      "approved": sum(item["approved"] for item in items),
                      "changed_topics": sum(len(changes[key]) for key in
                                            ("topics_added", "topics_removed", "topics_changed"))}}


def promote_candidate(root: Path, actor_id: int) -> dict:
    root = root.resolve()
    with writer_lock(root / ".iltkb"):
        pending = pending_candidate(root)
        if pending is None:
            raise ValueError("No candidate release awaits review")
        current = current_release(root)
        store = ReviewStore(root)
        with store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            actor = db.execute("SELECT role,active FROM users WHERE id=?", (actor_id,)).fetchone()
            if not actor or actor["role"] != "admin" or not actor["active"]:
                raise PermissionError("Active admin required")
            if current and current.name == pending["release"]:
                (root / ".iltkb" / "pending.json").unlink()
                db.execute("INSERT INTO audit(actor_id,action,target,detail,created_at) VALUES(?,?,?,?,?)",
                           (actor_id, "candidate_promotion_recovered", pending["release"], "{}", int(time.time())))
                return {"release": current.name, "already_published": True}
            if (current.name if current else None) != pending["base_release"]:
                raise ValueError("Published release changed; rebuild the candidate")
            report = candidate_report(root, review_store=store, review_db=db)
            if not report["ready"]:
                raise ValueError(f"Candidate still has {report['stats']['needs_review']} claims requiring approval")
            prior = store.reviews(pending["base_release"], db) if pending["base_release"] else {}
            carried = 0
            for item in report["items"]:
                if not item["carried_approval"]:
                    continue
                old = prior[item["id"]]
                db.execute("INSERT INTO reviews(release,claim_id,fingerprint,decision,flags,note,reviewer_id,updated_at,version,carried_from) "
                           "VALUES(?,?,?,?,?,?,?,?,?,?)",
                           (pending["release"], item["id"], old["fingerprint"], "approved", "[]", old["note"],
                            old["reviewer_id"], int(time.time()), 1, pending["base_release"]))
                carried += 1
            atomic_json(root / ".iltkb" / "current.json", {"release": pending["release"],
                                                           "manifest_sha256": pending["manifest_sha256"]})
            (root / ".iltkb" / "pending.json").unlink()
            db.execute("INSERT INTO audit(actor_id,action,target,detail,created_at) VALUES(?,?,?,?,?)",
                       (actor_id, "candidate_published", pending["release"],
                        json.dumps({"carried_approvals": carried}), int(time.time())))
        return {"release": pending["release"], "published": True}


def discard_candidate(root: Path, actor_id: int, reason: str) -> dict:
    if not isinstance(reason, str) or not 3 <= len(reason.strip()) <= 2000:
        raise ValueError("Discard reason must contain 3–2000 characters")
    root = root.resolve()
    with writer_lock(root / ".iltkb"):
        pending = pending_candidate(root)
        if pending is None:
            raise ValueError("No candidate release awaits review")
        current = current_release(root)
        if current and current.name == pending["release"]:
            raise ValueError("Candidate is already published; finish promotion instead")
        store = ReviewStore(root)
        with store.connect() as db:
            actor = db.execute("SELECT role,active FROM users WHERE id=?", (actor_id,)).fetchone()
            if not actor or actor["role"] != "admin" or not actor["active"]:
                raise PermissionError("Active admin required")
            db.execute("INSERT INTO audit(actor_id,action,target,detail,created_at) VALUES(?,?,?,?,?)",
                       (actor_id, "candidate_discarded", pending["release"], reason.strip(), int(time.time())))
        (root / ".iltkb" / "pending.json").unlink()
        return {"release": pending["release"], "discarded": True}
