"""Local knowledge release viewer with separate multi-user review management."""
from __future__ import annotations

from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie
from pathlib import Path
import gzip
import json
import mimetypes
import re
import secrets
import ssl
import threading
import time
from urllib.parse import parse_qs, unquote, urlsplit

from .management import ReviewConflict, ReviewStore
from .publication import candidate_report, pending_candidate, promote_candidate, discard_candidate
from .ontology import OntologyStore, published_graph, export_ntriples
from .incidents import active_ids, impact, incidents, quarantine, require_clear, resolve
from .retrieval import Reader, tokens
from .research import ResearchConflict, ResearchStore, assess as assess_research, continue_plan, plan as plan_research, suggest_gaps
from .storage import current_release, read_json, writer_lock


class Catalog:
    def __init__(self, root: Path):
        self.reader = Reader(root, allow_quarantine=True)
        self.release = self.reader.release.name
        self.manifest = self.reader.manifest
        self.sources = self.manifest["sources"]
        self.evidence = self.manifest["evidence"]
        self.pages = self.manifest["pages"]
        self.source_evidence: dict[str, list[dict]] = {sid: [] for sid in self.sources}
        for row in self.evidence.values():
            self.source_evidence[row["source_id"]].append(row)
        self.evidence_terms = {eid: tokens(row["statement"] + " " + row["quote"] + " " + row["section"])
                               for eid, row in self.evidence.items()}
        self.source_terms = {sid: tokens(source["title"] + " " + source["path"])
                             for sid, source in self.sources.items()}

    def overview(self) -> dict:
        groups = Counter(s.get("origin_group") or "Unknown source relationship" for s in self.sources.values())
        topics = []
        for tid, page in self.pages.items():
            topics.append({"id": tid, "title": page["topic"]["title"],
                           "group": page["topic"]["group"], "summary": page["summary"],
                           "claims": len(page["claims"]), "evidence": len(page["considered_evidence"]),
                           "gaps": len(page["gaps"])})
        sources = []
        for sid, source in self.sources.items():
            sources.append({"id": sid, "title": source["title"], "path": source["path"],
                            "origin_group": source.get("origin_group") or "Unknown source relationship",
                            "kind": "PDF" if source["path"].lower().endswith(".pdf") else "Markdown",
                            "visual_pending": len(source["visual_pending"]),
                            "evidence": len(self.source_evidence[sid]),
                            "pdf_pages_selected": source.get("pdf_pages_selected")})
        sources.sort(key=lambda row: row["title"])
        return {"release": self.release, "scope": self.manifest.get("scope", "text-only"),
                "stats": {"sources": len(self.sources), "evidence": len(self.evidence),
                          "claims": sum(len(p["claims"]) for p in self.pages.values()),
                          "topics": len(topics),
                          "visual_pending": sum(len(s["visual_pending"]) for s in self.sources.values())},
                "topics": topics, "sources": sources,
                "origin_groups": [{"name": key, "count": count} for key, count in groups.most_common()]}

    def topic(self, tid: str) -> dict | None:
        page = self.pages.get(tid)
        if page is None:
            return None
        claims = []
        for claim in page["claims"]:
            claims.append({"id": claim["id"], "kind": claim["kind"], "text": claim["text"],
                           "reasoning": claim["reasoning"], "conditions": claim["conditions"],
                           "evidence": claim["evidence"], "independence": claim["independence"],
                           "support_sources": claim["support_sources"]})
        return {"release": self.release, "id": tid, "title": page["topic"]["title"],
                "group": page["topic"]["group"], "summary": page["summary"],
                "claims": claims, "agreements": page["agreements"],
                "disagreements": page["disagreements"], "gaps": page["gaps"],
                "evidence_count": len(page["considered_evidence"])}

    def evidence_item(self, eid: str) -> dict | None:
        row = self.evidence.get(eid)
        if row is None:
            return None
        source = self.sources[row["source_id"]]
        return {"release": self.release, **row,
                "source": {"id": source["id"], "title": source["title"],
                           "path": source["path"], "origin_group": source.get("origin_group"),
                           "pdf_pages_selected": source.get("pdf_pages_selected")}}

    def source(self, sid: str) -> dict | None:
        source = self.sources.get(sid)
        if source is None:
            return None
        items = [{"id": e["id"], "statement": e["statement"], "page": e["page"],
                  "section": e["section"], "topics": e["topics"]} for e in self.source_evidence[sid]]
        return {"release": self.release, "id": sid, "title": source["title"],
                "path": source["path"], "sha256": source["sha256"],
                "original_url": "/api/source/" + sid + "/original",
                "origin_group": source.get("origin_group"), "status": source["status"],
                "pdf_pages_selected": source.get("pdf_pages_selected"),
                "visual_pending": source["visual_pending"],
                "coverage_count": len(source["coverage"]), "evidence": items}

    def search(self, question: str) -> dict:
        query = tokens(question)
        if not query:
            return {"release": self.release, "topics": [], "evidence": [], "sources": []}
        topics = self.reader.search(question, limit=5)
        matches = []
        for row in self.evidence.values():
            overlap = query & self.evidence_terms[row["id"]]
            score = len(overlap)
            if score:
                matches.append((score, row))
        matches.sort(key=lambda pair: (-pair[0], pair[1]["id"]))
        evidence = [{"id": row["id"], "statement": row["statement"],
                     "source_title": self.sources[row["source_id"]]["title"],
                     "page": row["page"], "score": score}
                    for score, row in matches[:12]]
        source_matches = []
        for sid, source in self.sources.items():
            score = len(query & self.source_terms[sid])
            if score:
                source_matches.append((score, sid, source))
        source_matches.sort(key=lambda pair: (-pair[0], pair[1]))
        sources = [{"id": sid, "title": source["title"], "score": score}
                   for score, sid, source in source_matches[:6]]
        return {"release": self.release, "topics": topics, "evidence": evidence, "sources": sources}


def create_server(root: Path, host: str = "127.0.0.1", port: int = 8765,
                  certfile: Path | None = None, keyfile: Path | None = None) -> ThreadingHTTPServer:
    root = root.resolve()
    if (certfile is None) != (keyfile is None):
        raise ValueError("Provide both certfile and keyfile for HTTPS")
    if certfile is None and host not in ("127.0.0.1", "::1", "localhost"):
        raise ValueError("HTTPS is required when serving beyond the local machine")
    # The first build may await review; management must be available before publication.
    web_root = Path(__file__).with_name("web")
    store = ReviewStore(root)
    research = ResearchStore(root)
    lock = threading.Lock()
    login_lock = threading.Lock()
    login_failures: dict[str, list[float]] = {}
    catalog: Catalog | None = None

    def current_catalog() -> Catalog:
        nonlocal catalog
        release = current_release(root)
        if release is None:
            raise ValueError("No published build")
        if catalog is None or catalog.release != release.name:
            with lock:
                if catalog is None or catalog.release != release.name:
                    catalog = Catalog(root)
        return catalog

    class Handler(BaseHTTPRequestHandler):
        def _token(self) -> str:
            try:
                cookie = SimpleCookie(self.headers.get("Cookie", ""))
                return cookie["iltkb_session"].value if "iltkb_session" in cookie else ""
            except Exception:
                return ""

        def _user(self) -> dict | None:
            return store.session(self._token())

        def _require(self, roles: tuple[str, ...] = ()) -> dict | None:
            user = self._user()
            if not user:
                self._json(401 if store.enabled() else 403, {"error": "Login required" if store.enabled() else "Set up an admin account first"})
                return None
            if roles and user["role"] not in roles:
                self._json(403, {"error": "Insufficient role"})
                return None
            return user

        def _body(self) -> dict:
            size = int(self.headers.get("Content-Length", "0"))
            if size < 1 or size > 3_000_000:
                raise ValueError("Invalid request size")
            data = json.loads(self.rfile.read(size))
            if not isinstance(data, dict):
                raise ValueError("Expected JSON object")
            return data

        def _cookie(self, token: str, *, clear: bool = False) -> None:
            value = f"iltkb_session={token}; Path=/; HttpOnly; SameSite=Strict"
            if certfile is not None:
                value += "; Secure"
            if clear:
                value += "; Max-Age=0"
            self.send_header("Set-Cookie", value)

        def _login_response(self, user: dict) -> None:
            token, csrf = store.create_session(user["id"])
            body = json.dumps({"user": {"username": user["username"], "role": user["role"]}, "csrf": csrf}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self._cookie(token)
            self.end_headers()
            self.wfile.write(body)

        def _claim(self, catalog: Catalog, claim_id: str) -> dict | None:
            for page in catalog.pages.values():
                for claim in page["claims"]:
                    if claim["id"] == claim_id:
                        return claim
            return None

        def _manage(self) -> dict:
            if current_release(root) is None:
                return {"release": None, "items": [], "history": [],
                        "stats": {"total": 0, "pending": 0, "flagged": 0, "approved": 0, "stale_reviews": 0}}
            cat = current_catalog()
            reviews = store.reviews(cat.release)
            items = []
            for tid, page in cat.pages.items():
                disagreements = set(page.get("disagreements", []))
                for index, claim in enumerate(page["claims"]):
                    saved = reviews.get(claim["id"])
                    auto = []
                    if claim["kind"] == "conflict":
                        auto.append("conflict")
                    if index in disagreements:
                        auto.append("inconsistent")
                    if saved and saved["fingerprint"] != store.fingerprint(claim):
                        auto.append("stale")
                    items.append({"id": claim["id"], "topic_id": tid,
                                  "topic": page["topic"]["title"], "text": claim["text"],
                                  "kind": claim["kind"], "evidence": claim["evidence"],
                                  "auto_flags": auto,
                                  "review": ({key: saved[key] for key in ("decision", "flags", "note", "username", "updated_at", "version")}
                                             if saved else None)})
            pending = sum(not item["review"] or item["review"]["decision"] == "pending" for item in items)
            flagged = sum(bool(item["auto_flags"] or (item["review"] and item["review"]["flags"])) for item in items)
            history = store.previous_reviews(cat.release)
            return {"release": cat.release, "items": items, "history": history,
                    "stats": {"total": len(items), "pending": pending, "flagged": flagged,
                              "approved": sum(bool(item["review"] and item["review"]["decision"] == "approved"
                                                   and not item["review"]["flags"] and not item["auto_flags"])
                                              for item in items),
                              "stale_reviews": len(history)}}

        def _send(self, code: int, body: bytes, content_type: str, *, cache: str = "no-store") -> None:
            compressible = content_type.startswith(("application/json", "application/javascript", "text/", "image/svg+xml"))
            if compressible and "gzip" in self.headers.get("Accept-Encoding", "") and len(body) > 1024:
                body = gzip.compress(body, compresslevel=5)
                compressed = True
            else:
                compressed = False
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            self.send_header("X-Content-Type-Options", "nosniff")
            if compressed:
                self.send_header("Content-Encoding", "gzip")
            self.end_headers()
            self.wfile.write(body)

        def _send_file(self, path: Path, content_type: str) -> None:
            size = path.stat().st_size
            start, end = 0, size - 1
            partial = False
            requested = self.headers.get("Range", "")
            if requested:
                match = re.fullmatch(r"bytes=(\d*)-(\d*)", requested.strip()) if len(requested) <= 100 else None
                if not match or (not match.group(1) and not match.group(2)):
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                first, last = match.groups()
                if first:
                    start = int(first)
                    end = min(int(last), size - 1) if last else size - 1
                else:
                    start = max(0, size - int(last))
                if start >= size or end < start:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                partial = True
            self.send_response(206 if partial else 200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            if partial:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            with path.open("rb") as stream:
                stream.seek(start)
                remaining = end - start + 1
                while remaining:
                    chunk = stream.read(min(65536, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)

        def _json(self, code: int, value) -> None:
            self._send(code, json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode(),
                       "application/json; charset=utf-8")

        def do_GET(self) -> None:
            parsed = urlsplit(self.path)
            path = unquote(parsed.path)
            try:
                if path == "/api/me":
                    user = self._user()
                    local_peers = {"127.0.0.1", "::1"}
                    if host not in ("0.0.0.0", "::"):
                        local_peers.add(host)
                    return self._json(200, {"enabled": store.enabled(),
                                            "has_release": current_release(root) is not None,
                                            "has_candidate": pending_candidate(root) is not None,
                                            "quarantined_sources": sorted(active_ids(root)),
                                            "setup_allowed": self.client_address[0] in local_peers,
                                            "user": {"username": user["username"], "role": user["role"]} if user else None,
                                            "csrf": user["csrf"] if user else None})
                if path.startswith("/api/") and not self._user():
                    return self._json(401, {"error": "Login required" if store.enabled() else "Set up an admin account first"})
                if path == "/api/manage/incidents":
                    if not self._require(("admin",)): return
                    return self._json(200, incidents(root))
                if path == "/api/manage/sources":
                    if not self._require(("admin",)): return
                    cat = current_catalog()
                    return self._json(200, {"sources": [{"id": sid, "path": source["path"], "title": source["title"]}
                                                        for sid, source in cat.sources.items()]})
                if path == "/api/manage/impact":
                    if not self._require(("admin",)): return
                    source_id = parse_qs(parsed.query).get("source_id", [""])[0]
                    try:
                        return self._json(200, impact(root, source_id))
                    except ValueError as exc:
                        return self._json(400, {"error": str(exc)})
                if path.startswith("/api/") and not path.startswith("/api/manage/"):
                    try:
                        require_clear(root)
                    except ValueError as exc:
                        return self._json(423, {"error": str(exc)})
                    if current_release(root) is None:
                        return self._json(409, {"error": "No published release; candidate review is required"})
                if path == "/api/manage/reviews":
                    if not self._require(("admin", "reviewer") if active_ids(root) else ()): return
                    return self._json(200, self._manage())
                if path == "/api/manage/candidate":
                    if not self._require(("admin", "reviewer")): return
                    return self._json(200, {"candidate": candidate_report(root)})
                if path == "/api/manage/ontology":
                    if not self._require(("admin", "reviewer")): return
                    return self._json(200, OntologyStore(root).report())
                if path == "/api/manage/users":
                    if not self._require(("admin",)): return
                    return self._json(200, {"users": store.users()})
                if path == "/api/manage/audit":
                    if not self._require(("admin",)): return
                    return self._json(200, {"events": store.audit()})
                if path == "/api/manage/markdown":
                    if not self._require(("admin", "reviewer")): return
                    published = {s["path"] for s in current_catalog().sources.values()} if current_release(root) else set()
                    files = [{"name": p.name, "size": p.stat().st_size, "published": p.name in published}
                             for p in sorted((root / "raw").glob("*")) if p.is_file() and p.suffix.lower() in (".md", ".markdown")]
                    return self._json(200, {"files": files})
                if path == "/api/manage/research":
                    if not self._require(("admin", "reviewer")): return
                    if active_ids(root): return self._json(423, {"error": "Resolve source quarantine before reading research tasks"})
                    return self._json(200, {"tasks": research.list(), "suggestions": suggest_gaps(root)})
                if path == "/api/manage/research/material":
                    if not self._require(("admin", "reviewer")): return
                    if active_ids(root): return self._json(423, {"error": "Source quarantine active"})
                    query = parse_qs(parsed.query)
                    filename, blob = research.material(query.get("task", [""])[0], query.get("id", [""])[0])
                    return self._send(200, blob, "text/plain; charset=utf-8")
                if path == "/api/overview":
                    return self._json(200, current_catalog().overview())
                if path == "/api/ontology":
                    return self._json(200, published_graph(root))
                if path == "/api/ontology.nt":
                    if published_graph(root)["status"] != "published":
                        return self._json(409, {"error": "Publish a current ontology snapshot before RDF export"})
                    return self._send(200, export_ntriples(root).encode("utf-8"),
                                      "application/n-triples; charset=utf-8")
                if path == "/api/status":
                    return self._json(200, {"release": current_catalog().release})
                if path == "/api/search":
                    question = parse_qs(parsed.query).get("q", [""])[0][:300].strip()
                    return self._json(200, current_catalog().search(question))
                if path.startswith("/api/topic/"):
                    cat = current_catalog()
                    data = cat.topic(path.removeprefix("/api/topic/"))
                    if data:
                        reviews = store.reviews(cat.release)
                        for index, claim in enumerate(data["claims"]):
                            saved = reviews.get(claim["id"])
                            claim["review"] = ({key: saved[key] for key in ("decision", "flags", "note", "username", "updated_at", "version")}
                                               if saved else None)
                            claim["auto_flags"] = (["conflict"] if claim["kind"] == "conflict" else []) + (
                                ["inconsistent"] if index in data["disagreements"] else [])
                    return self._json(200, data) if data else self._json(404, {"error": "Unknown topic"})
                if path.startswith("/api/evidence/"):
                    data = current_catalog().evidence_item(path.removeprefix("/api/evidence/"))
                    return self._json(200, data) if data else self._json(404, {"error": "Unknown evidence"})
                if path.startswith("/api/source/"):
                    if path.endswith("/original"):
                        sid = path.removeprefix("/api/source/").removesuffix("/original")
                        source = current_catalog().sources.get(sid)
                        if not source:
                            return self._json(404, {"error": "Unknown source"})
                        if not re.fullmatch(r"[0-9a-f]{64}", source["sha256"]):
                            return self._json(500, {"error": "Invalid source snapshot"})
                        snapshot = root / ".iltkb" / "objects" / source["sha256"]
                        mime = "application/pdf" if source["path"].lower().endswith(".pdf") else "text/markdown; charset=utf-8"
                        return self._send_file(snapshot, mime)
                    data = current_catalog().source(path.removeprefix("/api/source/"))
                    return self._json(200, data) if data else self._json(404, {"error": "Unknown source"})
                assets = {"/": "index.html", "/index.html": "index.html",
                          "/app.js": "app.js", "/manage.js": "manage.js", "/ontology.js": "ontology.js",
                          "/engineer.js": "engineer.js",
                          "/style.css": "style.css", "/favicon.svg": "favicon.svg"}
                if path in assets:
                    asset = web_root / assets[path]
                    mime = mimetypes.guess_type(asset.name)[0] or "application/octet-stream"
                    if mime.startswith("text/") or mime in ("application/javascript", "image/svg+xml"):
                        mime += "; charset=utf-8"
                    return self._send(200, asset.read_bytes(), mime, cache="no-cache")
                return self._json(404, {"error": "Not found"})
            except (ValueError, OSError, KeyError, TypeError):
                self._json(500, {"error": "Knowledge release unavailable"})

        def do_POST(self) -> None:
            path = urlsplit(self.path).path
            try:
                if self.headers.get("Content-Type", "").split(";", 1)[0] != "application/json":
                    return self._json(415, {"error": "JSON required"})
                data = self._body()
                if path == "/api/setup":
                    local_peers = {"127.0.0.1", "::1"}
                    if host not in ("0.0.0.0", "::"):
                        local_peers.add(host)
                    if store.enabled() or self.client_address[0] not in local_peers:
                        return self._json(403, {"error": "Setup unavailable"})
                    store.add_user(data.get("username", ""), data.get("password", ""), "admin")
                    identity = store.authenticate(data["username"], data["password"])
                    return self._login_response(identity)
                if path == "/api/login":
                    ip = self.client_address[0]
                    with login_lock:
                        recent = [stamp for stamp in login_failures.get(ip, []) if time.time() - stamp < 300]
                        login_failures[ip] = recent
                        if len(recent) >= 8:
                            return self._json(429, {"error": "Too many login attempts; retry later"})
                    user = store.authenticate(data.get("username", ""), data.get("password", ""))
                    if user:
                        with login_lock:
                            login_failures.pop(ip, None)
                        return self._login_response(user)
                    with login_lock:
                        login_failures[ip].append(time.time())
                    return self._json(401, {"error": "Invalid credentials"})
                user = self._require()
                if not user: return
                if not secrets.compare_digest(self.headers.get("X-CSRF-Token", ""), user["csrf"]):
                    return self._json(403, {"error": "Invalid CSRF token"})
                if path == "/api/logout":
                    store.end_session(self._token())
                    body = b'{}'
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self._cookie("", clear=True)
                    self.end_headers()
                    return self.wfile.write(body)
                if path == "/api/manage/quarantine":
                    if user["role"] != "admin": return self._json(403, {"error": "Admin role required"})
                    return self._json(201, quarantine(root, data.get("source_id"), data.get("reason"), user["username"]))
                if path == "/api/manage/resolve":
                    if user["role"] != "admin": return self._json(403, {"error": "Admin role required"})
                    return self._json(200, resolve(root, data.get("source_id"), data.get("reason"), user["username"]))
                if path == "/api/manage/review":
                    if user["role"] not in ("admin", "reviewer"):
                        return self._json(403, {"error": "Reviewer role required"})
                    with writer_lock(root / ".iltkb"):
                        release = data.get("release")
                        pending = pending_candidate(root)
                        if pending and release == pending["release"]:
                            manifest = read_json(pending["path"] / "manifest.json")
                            claim = next((claim for page in manifest["pages"].values()
                                          for claim in page["claims"] if claim["id"] == data.get("claim_id")), None)
                        else:
                            current = current_release(root)
                            if current is None or release != current.name:
                                return self._json(409, {"error": "Release changed; reload reviews"})
                            claim = self._claim(current_catalog(), data.get("claim_id", ""))
                        if claim is None:
                            return self._json(404, {"error": "Unknown claim"})
                        result = store.save_review(release, claim, data.get("decision"), data.get("flags"),
                                                   data.get("note", ""), user["id"], data.get("version"))
                    return self._json(200, result)
                if path == "/api/manage/candidate/publish":
                    if user["role"] != "admin": return self._json(403, {"error": "Admin role required"})
                    return self._json(200, promote_candidate(root, user["id"]))
                if path == "/api/manage/candidate/discard":
                    if user["role"] != "admin": return self._json(403, {"error": "Admin role required"})
                    return self._json(200, discard_candidate(root, user["id"], data.get("reason")))
                if path == "/api/manage/ontology/propose":
                    if user["role"] not in ("admin", "reviewer"):
                        return self._json(403, {"error": "Reviewer role required"})
                    result = OntologyStore(root).propose(user["id"], data.get("subject"), data.get("predicate"),
                                                         data.get("object"), data.get("evidence_ids"), data.get("note"))
                    return self._json(201, result)
                if path == "/api/manage/ontology/decision":
                    if user["role"] not in ("admin", "reviewer"):
                        return self._json(403, {"error": "Reviewer role required"})
                    result = OntologyStore(root).decide(user["id"], data.get("id"), data.get("decision"),
                                                        data.get("note"), data.get("version"))
                    return self._json(200, result)
                if path == "/api/manage/ontology/publish":
                    if user["role"] != "admin": return self._json(403, {"error": "Admin role required"})
                    require_clear(root)
                    return self._json(200, OntologyStore(root).publish(user["id"]))
                if path == "/api/manage/users":
                    if user["role"] != "admin": return self._json(403, {"error": "Admin role required"})
                    created = store.add_user(data.get("username", ""), data.get("password", ""),
                                             data.get("role", "viewer"), user["id"])
                    return self._json(201, created)
                if path == "/api/manage/user":
                    if user["role"] != "admin": return self._json(403, {"error": "Admin role required"})
                    store.set_user(int(data.get("id")), data.get("role"), data.get("active"), user["id"])
                    return self._json(200, {"ok": True})
                if path == "/api/manage/markdown":
                    if user["role"] not in ("admin", "reviewer"):
                        return self._json(403, {"error": "Reviewer role required"})
                    filename, content = data.get("filename"), data.get("content")
                    if (not isinstance(filename, str) or len(filename) > 128 or
                        not re.fullmatch(r"[\w][\w .()\-]{0,119}\.(?:md|markdown)", filename, re.I)):
                        return self._json(400, {"error": "Use a simple .md or .markdown filename"})
                    if not isinstance(content, str) or not content.strip() or len(content.encode()) > 2_000_000:
                        return self._json(400, {"error": "Markdown must be 1–2 MB of UTF-8 text"})
                    raw = root / "raw"
                    raw.mkdir(exist_ok=True)
                    target = raw / filename
                    try:
                        with target.open("x", encoding="utf-8") as stream:
                            stream.write(content)
                    except FileExistsError:
                        return self._json(409, {"error": "Filename already exists"})
                    with store.connect() as db:
                        db.execute("INSERT INTO audit(actor_id,action,target,detail,created_at) VALUES(?,?,?,?,?)",
                                   (user["id"], "markdown_uploaded", filename, json.dumps({"bytes": len(content.encode())}), int(time.time())))
                    return self._json(201, {"filename": filename, "published": False})
                if path.startswith("/api/manage/research/"):
                    if user["role"] not in ("admin", "reviewer"):
                        return self._json(403, {"error": "Reviewer role required"})
                    if active_ids(root):
                        return self._json(423, {"error": "Resolve source quarantine before research work"})
                    if path == "/api/manage/research/plan":
                        from .cli import configured_model
                        item = plan_research(root, data.get("question"), configured_model(root),
                                             topic_id=data.get("topic_id"), actor=user["username"])
                        return self._json(201, item)
                    if path == "/api/manage/research/material":
                        item = research.submit(data.get("task_id"), data.get("filename"),
                                               data.get("content"), user["username"], data.get("version"))
                        return self._json(201, item)
                    if path == "/api/manage/research/assess":
                        from .cli import configured_model
                        item = assess_research(root, data.get("task_id"), configured_model(root), actor=user["username"])
                        return self._json(200, item)
                    if path == "/api/manage/research/critique":
                        from .cli import configured_model
                        item = continue_plan(root, data.get("task_id"), configured_model(root), actor=user["username"])
                        return self._json(200, item)
                    if path == "/api/manage/research/decision":
                        item = research.decide(data.get("task_id"), data.get("decision"),
                                               data.get("note"), user["username"], data.get("version"))
                        return self._json(200, item)
                return self._json(404, {"error": "Not found"})
            except (ReviewConflict, ResearchConflict) as exc:
                self._json(409, {"error": str(exc)})
            except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
                self._json(400, {"error": str(exc)[:160]})
            except PermissionError:
                self._json(403, {"error": "Admin role required"})
            except OSError:
                self._json(500, {"error": "Research storage unavailable"})

        def log_message(self, format: str, *args) -> None:
            # Keep local logs free of search terms and source excerpts.
            pass

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    if certfile is not None:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(str(certfile), str(keyfile))
        server.socket = context.wrap_socket(server.socket, server_side=True)
    return server


def serve(root: Path, host: str = "127.0.0.1", port: int = 8765,
          certfile: Path | None = None, keyfile: Path | None = None) -> None:
    server = create_server(root, host, port, certfile, keyfile)
    scheme = "https" if certfile else "http"
    print(f"ILT KB web: {scheme}://{host}:{server.server_address[1]}/", flush=True)
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()
