"""Emergency source quarantine, independent of model calls and release builds."""
from __future__ import annotations

from pathlib import Path
import time

from .storage import atomic_json, current_release, digest, read_json, writer_lock


def _path(root: Path) -> Path:
    return root / ".iltkb" / "incidents.json"


def incidents(root: Path) -> dict:
    path = _path(root)
    data = read_json(path) if path.exists() else {"active": {}, "history": []}
    if not isinstance(data, dict) or not isinstance(data.get("active"), dict) or not isinstance(data.get("history"), list):
        raise ValueError("Invalid incident registry; knowledge access is blocked")
    return data


def active_ids(root: Path) -> set[str]:
    return set(incidents(root)["active"])


def require_clear(root: Path) -> None:
    active = active_ids(root)
    if active:
        raise ValueError("Knowledge access paused by source quarantine: " + ", ".join(sorted(active)))


def impact(root: Path, source_id: str) -> dict:
    release = current_release(root)
    if release is None:
        raise ValueError("No published release")
    manifest = read_json(release / "manifest.json")
    source = manifest["sources"].get(source_id)
    if source is None:
        raise ValueError("Unknown source ID in current release")
    evidence_ids = {eid for eid, row in manifest["evidence"].items() if row["source_id"] == source_id}
    topics = []
    for tid, page in manifest["pages"].items():
        if evidence_ids & set(page["considered_evidence"]):
            topics.append({"id": tid, "title": page["topic"]["title"],
                           "claims_at_risk": len(page["claims"]),
                           "direct_claim_ids": [c["id"] for c in page["claims"]
                                                if evidence_ids & set(c["evidence"])]})
    affected_topics = {topic["id"] for topic in topics}
    related_ids = {row["source_id"] for row in manifest["evidence"].values()
                   if row["source_id"] != source_id and affected_topics.intersection(row["topics"])}
    related_sources = [{"id": sid, "path": manifest["sources"][sid]["path"],
                        "same_declared_origin": bool(source.get("origin_group") and
                                                     source["origin_group"] == manifest["sources"][sid].get("origin_group"))}
                       for sid in sorted(related_ids)]
    return {"release": release.name, "source_id": source_id, "source_path": source["path"],
            "source_hash": source["sha256"], "evidence_ids": sorted(evidence_ids), "topics": topics,
            "related_sources_to_check": related_sources,
            "note": "All content in affected topics needs review, including claims without direct citations."}


def quarantine(root: Path, source_id: str, reason: str, actor: str = "cli") -> dict:
    if not isinstance(reason, str) or len(reason.strip()) < 3 or len(reason) > 2000:
        raise ValueError("Quarantine reason must contain 3–2000 characters")
    root = root.resolve()
    with writer_lock(root / ".iltkb" / "incident-lock"):
        data = incidents(root)
        if source_id in data["active"]:
            raise ValueError("Source is already quarantined")
        report = impact(root, source_id)
        data["active"][source_id] = {"reason": reason.strip(), "actor": actor,
                                      "at": int(time.time()), **report}
        atomic_json(_path(root), data)
        return data["active"][source_id]


def resolve(root: Path, source_id: str, note: str, actor: str = "cli") -> dict:
    if not isinstance(note, str) or len(note.strip()) < 3 or len(note) > 2000:
        raise ValueError("Resolution note must contain 3–2000 characters")
    root = root.resolve()
    with writer_lock(root / ".iltkb" / "incident-lock"):
        data = incidents(root)
        incident = data["active"].get(source_id)
        if incident is None:
            raise ValueError("Source is not quarantined")
        release = current_release(root)
        if release is None or release.name == incident["release"]:
            raise ValueError("Publish a corrected release before resolving quarantine")
        from .engine import validate_release
        validate_release(release, root / ".iltkb" / "objects")
        manifest = read_json(release / "manifest.json")
        old_hash = incident["source_hash"]
        if any(row["source_hash"] == old_hash for row in manifest["evidence"].values()):
            raise ValueError("Current release still contains evidence from the quarantined source version")
        for source in manifest["sources"].values():
            if source["sha256"] != old_hash:
                continue
            raw = root / "raw" / source["path"]
            if not raw.exists():
                continue
            meta_path = raw.with_suffix(raw.suffix + ".meta.json")
            meta = read_json(meta_path) if meta_path.exists() else {}
            if meta.get("status") != "withdrawn" or digest(raw.read_bytes()) != old_hash:
                raise ValueError("Persistently withdraw the unchanged source before resolving quarantine")
        from .management import ReviewStore
        store = ReviewStore(root)
        reviews = store.reviews(release.name)
        for affected in incident["topics"]:
            page = manifest["pages"].get(affected["id"])
            if page is None:
                continue
            for index, claim in enumerate(page["claims"]):
                review = reviews.get(claim["id"])
                if (not review or review["decision"] != "approved" or
                        review["flags"] or review["fingerprint"] != store.fingerprint(claim) or
                        claim["kind"] == "conflict" or index in page["disagreements"]):
                    raise ValueError("Approve every current claim in affected topics before resolving quarantine")
        record = {**incident, "resolved_by": actor, "resolved_at": int(time.time()), "resolved_release": release.name,
                  "resolution_note": note.strip()}
        data["history"].append(record)
        del data["active"][source_id]
        atomic_json(_path(root), data)
        return record
