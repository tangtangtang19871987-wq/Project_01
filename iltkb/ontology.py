"""A deliberately small, provenance-preserving RDFS/OWL-inspired semantic layer.

Only subclass, subproperty, domain, range and disjoint-class rules are implemented.
This is not a general OWL reasoner. Curated relations never become evidence.
"""
from __future__ import annotations

from collections import deque
from pathlib import Path
import json
import re
import sqlite3
import time
import uuid

from .management import ReviewStore
from .storage import atomic_json, current_release, digest, read_json, writer_lock


NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}\Z")
DEFAULT_SCHEMA = {
    "schema_version": 1,
    "classes": {
        "Resource": {"label": "Resource", "parent": None},
        "Claim": {"label": "Claim", "parent": "Resource"},
        "SourceStatement": {"label": "Source statement", "parent": "Claim"},
        "Inference": {"label": "Synthesis inference", "parent": "Claim"},
        "Conflict": {"label": "Unresolved conflict", "parent": "Claim"},
        "Evidence": {"label": "Original evidence", "parent": "Resource"},
        "Source": {"label": "Source", "parent": "Resource"},
        "Topic": {"label": "Topic", "parent": "Resource"},
        "Concept": {"label": "Domain concept", "parent": "Resource"},
        "Method": {"label": "Method", "parent": "Concept"},
        "ProcessCondition": {"label": "Process condition", "parent": "Concept"},
        "Metric": {"label": "Evaluation metric", "parent": "Concept"},
        "Assumption": {"label": "Assumption", "parent": "Concept"},
    },
    "properties": {
        "hasEvidence": {"label": "Cites evidence", "domain": "Claim", "range": "Evidence", "parent": None, "mode": "structural"},
        "fromSource": {"label": "From source", "domain": "Evidence", "range": "Source", "parent": None, "mode": "structural"},
        "aboutTopic": {"label": "About topic", "domain": "Claim", "range": "Topic", "parent": None, "mode": "structural"},
        "relatedTo": {"label": "Related to", "domain": "Claim", "range": "Claim", "parent": None, "mode": "curated"},
        "dependsOn": {"label": "Depends on", "domain": "Claim", "range": "Claim", "parent": "relatedTo", "mode": "curated"},
        "refines": {"label": "Refines", "domain": "Claim", "range": "Claim", "parent": "relatedTo", "mode": "curated"},
        "contradicts": {"label": "Contradicts", "domain": "Claim", "range": "Claim", "parent": "relatedTo", "mode": "curated"},
        "usesMethod": {"label": "Uses method", "domain": "Claim", "range": "Method", "parent": None, "mode": "curated"},
        "hasCondition": {"label": "Has condition", "domain": "Claim", "range": "ProcessCondition", "parent": None, "mode": "curated"},
        "measuresMetric": {"label": "Measures metric", "domain": "Claim", "range": "Metric", "parent": None, "mode": "curated"},
        "restsOnAssumption": {"label": "Rests on assumption", "domain": "Claim", "range": "Assumption", "parent": None, "mode": "curated"},
    },
    "disjoint": [["Claim", "Evidence"], ["Claim", "Source"], ["Claim", "Topic"],
                 ["Claim", "Concept"], ["Evidence", "Source"], ["Evidence", "Topic"],
                 ["Evidence", "Concept"], ["Source", "Topic"], ["Source", "Concept"],
                 ["Topic", "Concept"]],
    "entities": {},
}


def initialize_schema(root: Path) -> None:
    path = root / "schema" / "ontology.json"
    if not path.exists():
        atomic_json(path, DEFAULT_SCHEMA)


def _ancestors(name: str, definitions: dict) -> list[str]:
    result = [name]
    seen = {name}
    parent = definitions[name]["parent"]
    while parent is not None:
        if parent not in definitions or parent in seen:
            raise ValueError("Ontology hierarchy contains an unknown parent or cycle")
        result.append(parent)
        seen.add(parent)
        parent = definitions[parent]["parent"]
    return result


def _validate_schema(schema: dict) -> None:
    if not isinstance(schema, dict) or set(schema) != set(DEFAULT_SCHEMA) or schema["schema_version"] != 1:
        raise ValueError("Unsupported ontology schema")
    classes, properties, entities = (schema[key] for key in ("classes", "properties", "entities"))
    if not all(isinstance(value, dict) and len(value) <= 200 for value in (classes, properties, entities)):
        raise ValueError("Ontology classes, properties and entities must be bounded objects")
    for name, definition in classes.items():
        if (not isinstance(name, str) or not NAME.fullmatch(name) or not isinstance(definition, dict) or
            set(definition) != {"label", "parent"} or not isinstance(definition["label"], str) or
            not 1 <= len(definition["label"]) <= 120 or
            (definition["parent"] is not None and not isinstance(definition["parent"], str))):
            raise ValueError("Invalid ontology class")
        _ancestors(name, classes)
    for name, fixed in DEFAULT_SCHEMA["classes"].items():
        if name not in classes or classes[name]["parent"] != fixed["parent"]:
            raise ValueError("Built-in ontology class hierarchy cannot be changed")
    for name, definition in properties.items():
        if (not isinstance(name, str) or not NAME.fullmatch(name) or not isinstance(definition, dict) or
            set(definition) != {"label", "domain", "range", "parent", "mode"} or
            not isinstance(definition["label"], str) or not 1 <= len(definition["label"]) <= 120 or
            not isinstance(definition["domain"], str) or definition["domain"] not in classes or
            not isinstance(definition["range"], str) or definition["range"] not in classes or
            not isinstance(definition["mode"], str) or definition["mode"] not in ("structural", "curated") or
            (definition["parent"] is not None and not isinstance(definition["parent"], str))):
            raise ValueError("Invalid ontology property")
        if name not in DEFAULT_SCHEMA["properties"] and definition["mode"] != "curated":
            raise ValueError("Custom structural properties are not supported")
        chain = _ancestors(name, properties)
        for parent_name in chain[1:]:
            parent = properties[parent_name]
            if (definition["domain"] not in _ancestors(parent["domain"], classes) or
                definition["range"] not in _ancestors(parent["range"], classes)):
                raise ValueError("Subproperty domain/range must narrow its parent")
    for name, fixed in DEFAULT_SCHEMA["properties"].items():
        if name not in properties or any(properties[name][key] != fixed[key]
                                          for key in ("domain", "range", "parent", "mode")):
            raise ValueError("Built-in ontology property semantics cannot be changed")
    disjoint = schema["disjoint"]
    if not isinstance(disjoint, list) or len(disjoint) > 300:
        raise ValueError("Invalid disjoint classes")
    pairs = set()
    for pair in disjoint:
        if (not isinstance(pair, list) or len(pair) != 2 or any(not isinstance(item, str) or item not in classes for item in pair)):
            raise ValueError("Invalid disjoint class pair")
        left, right = pair
        if left in _ancestors(right, classes) or right in _ancestors(left, classes):
            raise ValueError("A class cannot be disjoint with its ancestor")
        pairs.add(tuple(sorted(pair)))
    if len(pairs) != len(disjoint) or not set(map(lambda pair: tuple(sorted(pair)), DEFAULT_SCHEMA["disjoint"])) <= pairs:
        raise ValueError("Built-in disjointness must be preserved without duplicates")
    for entity_id, entity in entities.items():
        if (not isinstance(entity_id, str) or not NAME.fullmatch(entity_id) or not isinstance(entity, dict) or
            set(entity) != {"label", "class"} or not isinstance(entity["label"], str) or
            not 1 <= len(entity["label"]) <= 200 or not isinstance(entity["class"], str) or
            entity["class"] not in classes or
            "Concept" not in _ancestors(entity["class"], classes)):
            raise ValueError("Entities must be named concepts with an ontology class")


def load_schema(root: Path) -> tuple[dict, str]:
    path = root / "schema" / "ontology.json"
    schema = read_json(path) if path.exists() else DEFAULT_SCHEMA
    _validate_schema(schema)
    return schema, digest(schema)


def _release_manifest(root: Path) -> tuple[Path, dict]:
    release = current_release(root)
    if release is None:
        raise ValueError("Publish a reviewed knowledge release before creating ontology relations")
    return release, read_json(release / "manifest.json")


def _nodes(manifest: dict, schema: dict, approved_claim_ids: set[str] | None = None) -> dict[str, dict]:
    nodes = {}
    kinds = {"source_statement": "SourceStatement", "inference": "Inference", "conflict": "Conflict"}
    for topic_id, page in manifest["pages"].items():
        nodes[f"topic:{topic_id}"] = {"class": "Topic", "label": page["topic"]["title"]}
        for claim in page["claims"]:
            if approved_claim_ids is not None and claim["id"] not in approved_claim_ids:
                continue
            nodes[f"claim:{claim['id']}"] = {"class": kinds[claim["kind"]], "label": claim["text"],
                                             "topic": topic_id, "evidence": claim["evidence"]}
    for evidence_id, row in manifest["evidence"].items():
        nodes[f"evidence:{evidence_id}"] = {"class": "Evidence", "label": row["statement"]}
    for source_id, row in manifest["sources"].items():
        nodes[f"source:{source_id}"] = {"class": "Source", "label": row["title"]}
    for entity_id, row in schema["entities"].items():
        nodes[f"entity:{entity_id}"] = {"class": row["class"], "label": row["label"]}
    return nodes


def _claim_approved(node_id: str, manifest: dict, reviews: dict) -> bool:
    if not node_id.startswith("claim:"):
        return True
    claim_id = node_id.split(":", 1)[1]
    for page in manifest["pages"].values():
        for index, claim in enumerate(page["claims"]):
            if claim["id"] != claim_id:
                continue
            review = reviews.get(claim_id)
            return bool(review and review["decision"] == "approved" and not review["flags"] and
                        review["fingerprint"] == ReviewStore.fingerprint(claim) and
                        claim["kind"] != "conflict" and index not in page["disagreements"])
    return False


def _approved_claim_ids(manifest: dict, reviews: dict) -> set[str]:
    approved = set()
    for page in manifest["pages"].values():
        for index, claim in enumerate(page["claims"]):
            review = reviews.get(claim["id"])
            if (review and review["decision"] == "approved" and not review["flags"] and
                review["fingerprint"] == ReviewStore.fingerprint(claim) and
                claim["kind"] != "conflict" and index not in page["disagreements"]):
                approved.add(claim["id"])
    return approved


def _validate_relation(row: dict, nodes: dict, schema: dict, manifest: dict,
                       reviews: dict | None = None) -> None:
    subject, predicate, object_id = (row.get(key) for key in ("subject", "predicate", "object"))
    if not all(isinstance(value, str) for value in (subject, predicate, object_id)):
        raise ValueError("Relation endpoints and predicate must be strings")
    property_def = schema["properties"].get(predicate)
    if not property_def or property_def["mode"] != "curated":
        raise ValueError("Only curated ontology properties can be proposed")
    if subject not in nodes or object_id not in nodes or subject == object_id:
        raise ValueError("Relation endpoints must be distinct nodes in the pinned release")
    classes = schema["classes"]
    if (property_def["domain"] not in _ancestors(nodes[subject]["class"], classes) or
        property_def["range"] not in _ancestors(nodes[object_id]["class"], classes)):
        raise ValueError("Relation endpoints do not satisfy the application shape")
    claim_nodes = [node for node in (subject, object_id) if node.startswith("claim:")]
    if not claim_nodes:
        raise ValueError("A curated relation must involve a claim")
    evidence_ids = row.get("evidence_ids")
    if (not isinstance(evidence_ids, list) or not 1 <= len(evidence_ids) <= 12 or
        any(not isinstance(eid, str) or eid not in manifest["evidence"] for eid in evidence_ids) or
        len(set(evidence_ids)) != len(evidence_ids)):
        raise ValueError("Relation needs 1–12 distinct original evidence IDs")
    for node in claim_nodes:
        if not set(evidence_ids) & set(nodes[node]["evidence"]):
            raise ValueError("Relation evidence must include evidence from each claim endpoint")
        if reviews is not None and not _claim_approved(node, manifest, reviews):
            raise ValueError("Relation depends on a claim without a current valid approval")
    if not set(evidence_ids) <= set().union(*(set(nodes[node]["evidence"]) for node in claim_nodes)):
        raise ValueError("Relation evidence must belong to its claim endpoints")


def _graph(manifest: dict, schema: dict, approved: list[dict],
           approved_claim_ids: set[str] | None = None) -> dict:
    nodes = _nodes(manifest, schema, approved_claim_ids)
    facts: dict[tuple[str, str, str], dict] = {}
    queue = deque()

    def add(subject: str, predicate: str, object_id: str, rule: str,
            premises: list[str] | None = None, assertion_id: str | None = None,
            evidence_ids: list[str] | None = None) -> None:
        key = (subject, predicate, object_id)
        if key in facts:
            return
        if len(facts) >= 50_000:
            raise ValueError("Ontology closure exceeds safe fact limit")
        fact_id = "T" + digest(key)[:24]
        facts[key] = {"id": fact_id, "subject": subject, "predicate": predicate, "object": object_id,
                      "rule": rule, "premises": premises or [], "assertion_id": assertion_id,
                      "evidence_ids": evidence_ids or []}
        queue.append(key)

    for node_id, node in sorted(nodes.items()):
        add(node_id, "rdf:type", node["class"], "declared_type")
    for topic_id, page in sorted(manifest["pages"].items()):
        for claim in page["claims"]:
            claim_node = f"claim:{claim['id']}"
            if claim_node not in nodes:
                continue
            add(claim_node, "aboutTopic", f"topic:{topic_id}", "compiler_structure")
            for evidence_id in claim["evidence"]:
                add(claim_node, "hasEvidence", f"evidence:{evidence_id}", "compiler_provenance", evidence_ids=[evidence_id])
    for evidence_id, row in sorted(manifest["evidence"].items()):
        add(f"evidence:{evidence_id}", "fromSource", f"source:{row['source_id']}",
            "compiler_provenance", evidence_ids=[evidence_id])
    for row in sorted(approved, key=lambda item: item["id"]):
        add(row["subject"], row["predicate"], row["object"], "approved_relation",
            assertion_id=row["id"], evidence_ids=row["evidence_ids"])
    while queue:
        subject, predicate, object_id = key = queue.popleft()
        premise = facts[key]["id"]
        if predicate == "rdf:type":
            parent = schema["classes"][object_id]["parent"]
            if parent:
                add(subject, "rdf:type", parent, "rdfs:subClassOf", [premise])
        else:
            definition = schema["properties"][predicate]
            if definition["parent"]:
                add(subject, definition["parent"], object_id, "rdfs:subPropertyOf", [premise])
            add(subject, "rdf:type", definition["domain"], "rdfs:domain", [premise])
            add(object_id, "rdf:type", definition["range"], "rdfs:range", [premise])
    issues = []
    for node_id in sorted(nodes):
        for left, right in schema["disjoint"]:
            first, second = facts.get((node_id, "rdf:type", left)), facts.get((node_id, "rdf:type", right))
            if first and second:
                issues.append({"kind": "owl:disjointWith", "node": node_id,
                               "classes": [left, right], "premises": [first["id"], second["id"]]})
    ordered = sorted(facts.values(), key=lambda fact: (fact["subject"], fact["predicate"], fact["object"]))
    return {"nodes": nodes, "facts": ordered, "issues": issues,
            "stats": {"nodes": len(nodes), "asserted_relations": len(approved),
                      "facts": len(ordered), "derived_facts": sum(fact["rule"].startswith("rdfs:") for fact in ordered),
                      "approved_claims": sum(node["class"] in ("SourceStatement", "Inference", "Conflict")
                                             for node in nodes.values()),
                      "omitted_claims": sum(len(page["claims"]) for page in manifest["pages"].values()) -
                                        sum(node["class"] in ("SourceStatement", "Inference", "Conflict")
                                            for node in nodes.values())}}


class OntologyStore:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.path = self.root / ".iltkb" / "ontology.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS relations (
                  id TEXT PRIMARY KEY, release TEXT NOT NULL, schema_hash TEXT NOT NULL,
                  subject TEXT NOT NULL, predicate TEXT NOT NULL, object TEXT NOT NULL,
                  evidence_ids TEXT NOT NULL, note TEXT NOT NULL, status TEXT NOT NULL,
                  proposer_id INTEGER NOT NULL, reviewer_id INTEGER, decision_note TEXT NOT NULL,
                  version INTEGER NOT NULL, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
                CREATE INDEX IF NOT EXISTS idx_ontology_release ON relations(release,schema_hash);
                CREATE TABLE IF NOT EXISTS events (
                  id INTEGER PRIMARY KEY, actor_id INTEGER NOT NULL, action TEXT NOT NULL,
                  target TEXT NOT NULL, detail TEXT NOT NULL, created_at INTEGER NOT NULL);
            """)
        self.path.chmod(0o600)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=10000")
        return db

    def _require_actor(self, actor_id: int, *, admin: bool = False) -> None:
        with ReviewStore(self.root).connect() as db:
            actor = db.execute("SELECT role,active FROM users WHERE id=?", (actor_id,)).fetchone()
        if not actor or not actor["active"] or (actor["role"] != "admin" if admin else actor["role"] not in ("admin", "reviewer")):
            raise PermissionError("Active admin required" if admin else "Active reviewer required")

    @staticmethod
    def _rows(db, release: str, schema_hash: str) -> list[dict]:
        rows = db.execute("SELECT * FROM relations WHERE release=? AND schema_hash=? ORDER BY created_at,id",
                          (release, schema_hash))
        return [{**dict(row), "evidence_ids": json.loads(row["evidence_ids"])} for row in rows]

    def propose(self, actor_id: int, subject: str, predicate: str, object_id: str,
                evidence_ids: list[str], note: str) -> dict:
        if not isinstance(note, str) or not 3 <= len(note.strip()) <= 4000:
            raise ValueError("Relation rationale must contain 3–4000 characters")
        with writer_lock(self.root / ".iltkb"):
            self._require_actor(actor_id)
            release, manifest = _release_manifest(self.root)
            schema, schema_hash = load_schema(self.root)
            row = {"subject": subject, "predicate": predicate, "object": object_id,
                   "evidence_ids": evidence_ids}
            _validate_relation(row, _nodes(manifest, schema), schema, manifest)
            with self.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                if db.execute("SELECT 1 FROM relations WHERE release=? AND schema_hash=? AND subject=? AND predicate=? AND object=? AND status<>'rejected'",
                              (release.name, schema_hash, subject, predicate, object_id)).fetchone():
                    raise ValueError("This relation is already proposed or approved")
                relation_id, now = uuid.uuid4().hex, int(time.time())
                db.execute("INSERT INTO relations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (relation_id, release.name, schema_hash, subject, predicate, object_id,
                            json.dumps(evidence_ids), note.strip(), "proposed", actor_id, None, "", 1, now, now))
                db.execute("INSERT INTO events(actor_id,action,target,detail,created_at) VALUES(?,?,?,?,?)",
                           (actor_id, "relation_proposed", relation_id, "{}", now))
            return {"id": relation_id, "release": release.name, "status": "proposed", "version": 1}

    def decide(self, actor_id: int, relation_id: str, decision: str,
               note: str, expected_version: int) -> dict:
        if decision not in ("approved", "rejected") or type(expected_version) is not int or expected_version < 1:
            raise ValueError("Invalid relation decision or version")
        if not isinstance(note, str) or not 3 <= len(note.strip()) <= 4000:
            raise ValueError("Decision note must contain 3–4000 characters")
        with writer_lock(self.root / ".iltkb"):
            self._require_actor(actor_id)
            release, manifest = _release_manifest(self.root)
            schema, schema_hash = load_schema(self.root)
            with self.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                raw = db.execute("SELECT * FROM relations WHERE id=?", (relation_id,)).fetchone()
                if not raw or raw["release"] != release.name or raw["schema_hash"] != schema_hash:
                    raise ValueError("Relation is missing or stale for this release/schema")
                if raw["version"] != expected_version or raw["status"] not in ("proposed", "approved"):
                    raise ValueError("Relation changed; reload before deciding")
                row = {**dict(raw), "evidence_ids": json.loads(raw["evidence_ids"])}
                if decision == "approved":
                    if actor_id == raw["proposer_id"]:
                        raise ValueError("A relation proposer cannot approve their own proposal")
                    reviews = ReviewStore(self.root).reviews(release.name)
                    _validate_relation(row, _nodes(manifest, schema), schema, manifest, reviews)
                    others = [item for item in self._rows(db, release.name, schema_hash)
                              if item["status"] == "approved" and item["id"] != relation_id]
                    graph = _graph(manifest, schema, others + [row])
                    if graph["issues"]:
                        raise ValueError("Relation would make the ontology inconsistent")
                now = int(time.time())
                db.execute("UPDATE relations SET status=?,reviewer_id=?,decision_note=?,version=version+1,updated_at=? WHERE id=?",
                           (decision, actor_id, note.strip(), now, relation_id))
                db.execute("INSERT INTO events(actor_id,action,target,detail,created_at) VALUES(?,?,?,?,?)",
                           (actor_id, "relation_" + decision, relation_id, "{}", now))
            return {"id": relation_id, "status": decision, "version": expected_version + 1}

    def report(self) -> dict:
        release, manifest = _release_manifest(self.root)
        schema, schema_hash = load_schema(self.root)
        with self.connect() as db:
            relations = self._rows(db, release.name, schema_hash)
            stale_count = db.execute("SELECT count(*) FROM relations WHERE release<>? OR schema_hash<>?",
                                     (release.name, schema_hash)).fetchone()[0]
        reviews = ReviewStore(self.root).reviews(release.name)
        nodes = _nodes(manifest, schema)
        blocked = []
        approved = []
        for row in relations:
            if row["status"] != "approved":
                continue
            try:
                _validate_relation(row, nodes, schema, manifest, reviews)
                approved.append(row)
            except ValueError as exc:
                blocked.append({"id": row["id"], "reason": str(exc)})
        graph = _graph(manifest, schema, approved, _approved_claim_ids(manifest, reviews))
        published = self.published_status()
        return {"release": release.name, "schema": schema, "schema_hash": schema_hash,
                "nodes": {key: value for key, value in nodes.items()
                          if key.startswith(("claim:", "entity:"))},
                "relations": relations, "stale_count": stale_count,
                "preview": {"stats": graph["stats"], "issues": graph["issues"], "blocked": blocked},
                "ready": graph["stats"]["approved_claims"] > 0 and not graph["issues"] and not blocked,
                "published": {key: value for key, value in published.items() if key != "graph" and key != "schema"}}

    def publish(self, actor_id: int) -> dict:
        with writer_lock(self.root / ".iltkb"):
            self._require_actor(actor_id, admin=True)
            release, manifest = _release_manifest(self.root)
            from .engine import validate_release
            validate_release(release, self.root / ".iltkb" / "objects")
            schema, schema_hash = load_schema(self.root)
            review_store = ReviewStore(self.root)
            with review_store.connect() as review_db:
                review_db.execute("BEGIN IMMEDIATE")
                with self.connect() as db:
                    db.execute("BEGIN IMMEDIATE")
                    relations = [row for row in self._rows(db, release.name, schema_hash) if row["status"] == "approved"]
                    reviews = review_store.reviews(release.name, review_db)
                    nodes = _nodes(manifest, schema)
                    dependencies = {}
                    for row in relations:
                        _validate_relation(row, nodes, schema, manifest, reviews)
                        for node in (row["subject"], row["object"]):
                            if node.startswith("claim:"):
                                claim_id = node.split(":", 1)[1]
                                dependencies[claim_id] = {"fingerprint": reviews[claim_id]["fingerprint"],
                                                          "version": reviews[claim_id]["version"]}
                    approved_claim_ids = _approved_claim_ids(manifest, reviews)
                    if not approved_claim_ids:
                        raise ValueError("Approve at least one current claim before publishing an ontology snapshot")
                    for claim_id in approved_claim_ids:
                        if claim_id not in dependencies:
                            dependencies[claim_id] = {"fingerprint": reviews[claim_id]["fingerprint"],
                                                      "version": reviews[claim_id]["version"]}
                    graph = _graph(manifest, schema, relations, approved_claim_ids)
                    if graph["issues"]:
                        raise ValueError("Ontology contains disjoint-class inconsistency")
                    snapshot_id = uuid.uuid4().hex
                    snapshot = {"id": snapshot_id, "release": release.name,
                                "manifest_sha256": digest((release / "manifest.json").read_bytes()),
                                "schema_hash": schema_hash, "schema": schema, "relations": relations,
                                "review_dependencies": dependencies, "graph": graph}
                    folder = self.root / ".iltkb" / "ontology" / "snapshots"
                    path = folder / f"{snapshot_id}.json"
                    if path.exists():
                        raise ValueError("Ontology snapshot ID collision")
                    atomic_json(path, snapshot)
                    checksum = digest(path.read_bytes())
                    atomic_json(self.root / ".iltkb" / "ontology" / "current.json",
                                {"id": snapshot_id, "sha256": checksum, "release": release.name})
                    db.execute("INSERT INTO events(actor_id,action,target,detail,created_at) VALUES(?,?,?,?,?)",
                               (actor_id, "ontology_published", snapshot_id,
                                json.dumps({"relations": len(relations)}), int(time.time())))
            return {"snapshot": snapshot_id, "release": release.name, "relations": len(relations)}

    def published_status(self) -> dict:
        pointer = self.root / ".iltkb" / "ontology" / "current.json"
        if not pointer.exists():
            return {"status": "unpublished"}
        data = read_json(pointer)
        if (not isinstance(data, dict) or not isinstance(data.get("id"), str) or
            not re.fullmatch(r"[0-9a-f]{32}", data["id"]) or not isinstance(data.get("sha256"), str) or
            not re.fullmatch(r"[0-9a-f]{64}", data["sha256"])):
            raise ValueError("Invalid ontology pointer")
        path = self.root / ".iltkb" / "ontology" / "snapshots" / (data["id"] + ".json")
        if (not path.is_file() or path.is_symlink() or
            not path.resolve().is_relative_to((self.root / ".iltkb" / "ontology").resolve()) or
            digest(path.read_bytes()) != data["sha256"]):
            raise ValueError("Ontology snapshot hash mismatch")
        snapshot = read_json(path)
        if data.get("release") != snapshot.get("release") or data["id"] != snapshot.get("id"):
            raise ValueError("Ontology pointer and snapshot disagree")
        release, manifest = _release_manifest(self.root)
        schema, schema_hash = load_schema(self.root)
        if (snapshot["release"] != release.name or snapshot["schema_hash"] != schema_hash or
            snapshot["manifest_sha256"] != digest((release / "manifest.json").read_bytes())):
            return {"status": "stale", "reason": "Release or ontology schema changed"}
        with self.connect() as db:
            rows = {row["id"]: row for row in self._rows(db, release.name, schema_hash)}
        for saved in snapshot["relations"]:
            live = rows.get(saved["id"])
            if not live or any(live[key] != saved[key] for key in
                               ("subject", "predicate", "object", "evidence_ids", "status", "version")):
                return {"status": "stale", "reason": "A published relation changed"}
        reviews = ReviewStore(self.root).reviews(release.name)
        for claim_id, saved in snapshot["review_dependencies"].items():
            live = reviews.get(claim_id)
            if not live or live["version"] != saved["version"] or live["fingerprint"] != saved["fingerprint"] or not _claim_approved("claim:" + claim_id, manifest, reviews):
                return {"status": "stale", "reason": "A supporting claim approval changed"}
        return {"status": "published", "snapshot": snapshot["id"], "release": release.name,
                "schema_hash": schema_hash, "graph": snapshot["graph"],
                "schema": schema}


def published_graph(root: Path) -> dict:
    from .incidents import require_clear
    require_clear(root)
    return OntologyStore(root).published_status()


def export_ntriples(root: Path) -> str:
    """Serialize a published snapshot as RDF 1.1 N-Triples without parsing external RDF."""
    published = published_graph(root)
    if published["status"] != "published":
        raise ValueError("Publish a current ontology snapshot before RDF export")
    graph, schema = published["graph"], published["schema"]
    release, schema_hash = published["release"], published["schema_hash"]
    rdf = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
    rdfs = "http://www.w3.org/2000/01/rdf-schema#"
    owl = "http://www.w3.org/2002/07/owl#"
    prov = "http://www.w3.org/ns/prov#"

    def local(kind: str, name: str) -> str:
        scope = schema_hash if kind in ("class", "property") else release
        return "urn:uuid:" + str(uuid.uuid5(uuid.NAMESPACE_URL, f"iltkb:{scope}:{kind}:{name}"))

    def iri(value: str) -> str:
        return f"<{value}>"

    def literal(value: str) -> str:
        escapes = {'"': '\\"', "\\": "\\\\", "\n": "\\n", "\r": "\\r", "\t": "\\t"}
        return '"' + "".join(escapes.get(char, f"\\u{ord(char):04X}" if ord(char) < 32 else char)
                             for char in value) + '"'

    triples = set()

    def add(subject: str, predicate: str, object_id: str, *, object_literal: bool = False) -> None:
        triples.add(f"{iri(subject)} {iri(predicate)} {literal(object_id) if object_literal else iri(object_id)} .")

    for name, definition in schema["classes"].items():
        subject = local("class", name)
        add(subject, rdf + "type", rdfs + "Class")
        add(subject, rdfs + "label", definition["label"], object_literal=True)
        if definition["parent"]:
            add(subject, rdfs + "subClassOf", local("class", definition["parent"]))
    for name, definition in schema["properties"].items():
        subject = local("property", name)
        add(subject, rdf + "type", rdf + "Property")
        add(subject, rdfs + "label", definition["label"], object_literal=True)
        add(subject, rdfs + "domain", local("class", definition["domain"]))
        add(subject, rdfs + "range", local("class", definition["range"]))
        if definition["parent"]:
            add(subject, rdfs + "subPropertyOf", local("property", definition["parent"]))
    for left, right in schema["disjoint"]:
        add(local("class", left), owl + "disjointWith", local("class", right))
    for node_id, node in graph["nodes"].items():
        add(local("node", node_id), rdfs + "label", node["label"], object_literal=True)
    for fact in graph["facts"]:
        subject = local("node", fact["subject"])
        predicate = rdf + "type" if fact["predicate"] == "rdf:type" else local("property", fact["predicate"])
        object_id = (local("class", fact["object"]) if fact["predicate"] == "rdf:type"
                     else local("node", fact["object"]))
        add(subject, predicate, object_id)
        statement = local("statement", fact["id"])
        add(statement, rdf + "type", rdf + "Statement")
        add(statement, rdf + "subject", subject)
        add(statement, rdf + "predicate", predicate)
        add(statement, rdf + "object", object_id)
        add(statement, rdfs + "comment", fact["rule"], object_literal=True)
        for evidence_id in fact["evidence_ids"]:
            add(statement, prov + "wasDerivedFrom", local("node", "evidence:" + evidence_id))
        for premise in fact["premises"]:
            add(statement, prov + "wasDerivedFrom", local("statement", premise))
    return "\n".join(sorted(triples)) + "\n"
