"""Synthetic fixtures test invariants, not ILT scientific validity."""
import copy
from http.cookiejar import CookieJar
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import build_opener, HTTPCookieProcessor, Request, urlopen

from iltkb.documents import chunks, load_document
from iltkb.engine import build, initialize, realign_quote, validate_release
from iltkb.model import OllamaModel
from iltkb.retrieval import Reader, tokens
from iltkb.publication import candidate_report, discard_candidate, promote_candidate
from iltkb.management import ReviewStore
from iltkb.storage import atomic_json, current_release, digest, read_json, writer_lock


class ScriptedModel:
    fingerprint = {"model": "synthetic-test-only", "revision": "1"}

    def __init__(self):
        self.calls = []
        self.fail = None

    def complete(self, task, payload):
        self.calls.append(task)
        if task == "extract":
            quote = payload["text"]
            return {"items": [{"quote": "fabricated" if self.fail == "quote" else quote,
                                "statement": quote, "topics": ["optimization"],
                                "conditions": {"scope": "synthetic test"}}]}
        if task == "extract_review":
            return {"coverage_ok": True, "issues": []}
        if task == "synthesize":
            refs = list(payload["evidence"])
            claims = [{"kind": "inference", "text": "Synthetic example: optimization conditions differ.",
                       "evidence": ["C-generated"] if self.fail == "circular" else refs,
                       "conditions": {"scope": "synthetic"}, "reasoning": "For testing evidence relationships only; not a technical conclusion."}]
            return {"summary": "Synthetic optimization test materials", "keywords": ["optimization", "conditions"],
                    "claims": claims, "agreements": [], "disagreements": [],
                    "gaps": ["All materials are synthetic test data"],
                    "considered_evidence": refs[:-1] if self.fail == "omission" else refs}
        if task == "review":
            return {"claims": [{"index": i, "supported": self.fail != "review", "reason": "synthetic fixture"}
                               for i, _ in enumerate(payload["candidate"]["claims"])],
                    "coverage_ok": True, "issues": []}
        if task == "answer":
            eid = "Eunknown" if self.fail == "answer" else next(iter(payload["evidence"]))
            return {"answer": f"Synthetic test conclusion.[E:{eid}]", "evidence": [eid]}
        raise AssertionError(task)


class ResearchModel(ScriptedModel):
    def complete(self, task, payload):
        if task == "research_plan":
            self.calls.append(task)
            ids = list(payload["evidence"])
            return {"hypothesis": "Synthetic hypothesis: differing conditions may affect results; unverified.",
                    "support_evidence": ids[:1] if self.fail != "research_unknown" else ["E-fabricated"],
                    "counter_evidence": ids[1:2], "uncertainty": "Reproducible raw data is missing.",
                    "requests": [{"kind": "validation", "question": "Provide a synthetic validation protocol and data access",
                                  "why": "Current evidence contains text statements only", "acceptance": "Provide inputs, outputs, parameters, and reproduction steps"}]}
        if task == "research_assess":
            self.calls.append(task)
            return {"assessment": "potentially_supported", "reason": "For review as a candidate only.",
                    "evidence": list(payload["original_evidence"])[:1],
                    "materials": [row["sha256"] for row in payload["materials"]] if self.fail != "research_bad_hash" else ["0" * 64],
                    "remaining_gap": "An engineer must still run validation and check data provenance."}
        if task == "research_critique":
            self.calls.append(task)
            extra = list(payload["additional_evidence"])
            return {"verdict": "insufficient", "challenge": "Different conditions could explain the difference; this does not confirm the hypothesis.",
                    "counter_evidence": ["E-fabricated"] if self.fail == "research_bad_critique" else extra[:1],
                    "assumptions": ["Experimental conditions are comparable; synthetic test only."],
                    "next_check": "Provide raw experimental records under matching conditions."}
        return super().complete(task, payload)


class SystemTests(unittest.TestCase):
    def test_research_graph_stays_outside_release_and_needs_human_decision(self):
        from iltkb.research import ResearchConflict, ResearchStore, assess, plan, suggest_gaps
        model = ResearchModel()
        build(self.root, model)
        published = current_release(self.root).name
        self.assertTrue(suggest_gaps(self.root))
        task = plan(self.root, "How should synthetic optimization conditions be compared?", model, topic_id="optimization")
        store = ResearchStore(self.root)
        calls_before = len(model.calls)
        self.assertEqual(plan(self.root, "How should synthetic optimization conditions be compared?", model, topic_id="optimization")["id"], task["id"])
        self.assertEqual(len(model.calls), calls_before)
        self.assertEqual(task["status"], "awaiting_material")
        self.assertIn("research_critique", model.calls)
        self.assertTrue(task["candidate"]["critique"]["challenge"])
        self.assertEqual(task["release"], published)
        self.assertTrue(task["candidate"]["requests"])
        self.assertEqual(current_release(self.root).name, published)
        with self.assertRaises(ValueError):
            store.decide(task["id"], "accept_for_followup", "Not reviewed", "reviewer", task["version"])
        task = store.submit(task["id"], "protocol.md", "# Synthetic validation protocol\nTest only; not an experiment.", "engineer", task["version"])
        self.assertEqual(task["status"], "awaiting_assessment")
        with self.assertRaises(ResearchConflict):
            store.submit(task["id"], "extra.txt", "stale", "engineer", task["version"] - 1)
        self.assertFalse((self.root / "raw" / "protocol.md").exists())
        name, blob = store.material(task["id"], task["materials"][0]["id"])
        self.assertEqual(name, "protocol.md")
        self.assertIn(b"#", blob)
        task = assess(self.root, task["id"], model)
        self.assertEqual(task["status"], "awaiting_review")
        task = store.decide(task["id"], "accept_for_followup", "Schedule follow-up validation only", "reviewer", task["version"])
        self.assertEqual(task["status"], "followup_accepted")
        self.assertEqual(current_release(self.root).name, published)
        self.assertEqual(len(store.list()), 1)

    def test_research_rejects_fabricated_ids_bad_hash_and_stale_release(self):
        from iltkb.research import ResearchConflict, ResearchStore, assess, plan
        model = ResearchModel()
        build(self.root, model)
        model.fail = "research_unknown"
        with self.assertRaisesRegex(ValueError, "unknown"):
            plan(self.root, "How should synthetic optimization conditions be compared?", model)
        self.assertEqual(ResearchStore(self.root).list(), [])
        model.fail = None
        with self.assertRaisesRegex(ValueError, "original source"):
            plan(self.root, "quasar-absent", model)
        model.fail = "research_bad_critique"
        with self.assertRaisesRegex(ValueError, "unknown"):
            plan(self.root, "Another synthetic optimization research question?", model)
        draft = ResearchStore(self.root).list()[0]
        self.assertEqual(draft["status"], "awaiting_critique")
        proposals_before = model.calls.count("research_plan")
        model.fail = None
        resumed = plan(self.root, "Another synthetic optimization research question?", model)
        self.assertEqual(resumed["id"], draft["id"])
        self.assertEqual(resumed["status"], "awaiting_material")
        self.assertEqual(model.calls.count("research_plan"), proposals_before)
        task = plan(self.root, "How should synthetic optimization conditions be compared?", model)
        task = ResearchStore(self.root).submit(task["id"], "protocol.md", "# Synthetic material", "engineer", task["version"])
        model.fail = "research_bad_hash"
        with self.assertRaisesRegex(ValueError, "unknown"):
            assess(self.root, task["id"], model)
        self.assertEqual(ResearchStore(self.root).get(task["id"])["status"], "awaiting_assessment")
        self.write("new.md", "# New material\nNew synthetic test content.")
        model.fail = None
        build(self.root, model)
        with self.assertRaises(ResearchConflict):
            assess(self.root, task["id"], model)
        with self.assertRaises(ResearchConflict):
            ResearchStore(self.root).submit(task["id"], "late.txt", "new", "engineer", task["version"])

    def test_legacy_research_proposal_requires_critique_before_material(self):
        from iltkb.research import ResearchStore, continue_plan, plan
        model = ResearchModel()
        build(self.root, model)
        task = plan(self.root, "Synthetic old-release research question", model, topic_id="optimization")
        candidate = dict(task["candidate"])
        candidate.pop("critique")
        with ResearchStore(self.root).connect() as db:
            db.execute("UPDATE tasks SET candidate=?,status='awaiting_material' WHERE id=?",
                       (json.dumps(candidate), task["id"]))
        migrated = ResearchStore(self.root).get(task["id"])
        self.assertEqual(migrated["status"], "awaiting_critique")
        self.assertTrue(any(event["action"] == "legacy_critique_required" for event in migrated["events"]))
        self.assertEqual(continue_plan(self.root, task["id"], model)["status"], "awaiting_material")

    def test_research_web_roles_csrf_and_material_review(self):
        from iltkb.research import assess, plan
        from iltkb.webapp import create_server
        model = ResearchModel()
        build(self.root, model)
        task = plan(self.root, "How can the synthetic conditions be validated?", model)
        server = create_server(self.root, port=0)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        admin = build_opener(HTTPCookieProcessor(CookieJar()))
        viewer = build_opener(HTTPCookieProcessor(CookieJar()))

        def call(opener, path, body=None, csrf=None):
            headers = {"Content-Type": "application/json"} if body is not None else {}
            if csrf is not None:
                headers["X-CSRF-Token"] = csrf
            request = Request(base + path, data=json.dumps(body).encode() if body is not None else None,
                              headers=headers)
            with opener.open(request) as response:
                return json.load(response)

        try:
            with self.assertRaises(HTTPError) as blocked:
                call(admin, "/api/manage/research")
            self.assertEqual(blocked.exception.code, 401)
            setup = call(admin, "/api/setup", {"username": "owner", "password": "strong-local-password"})
            call(admin, "/api/manage/users", {"username": "reader", "password": "another-strong-password", "role": "viewer"}, setup["csrf"])
            call(viewer, "/api/login", {"username": "reader", "password": "another-strong-password"})
            with self.assertRaises(HTTPError) as blocked:
                call(viewer, "/api/manage/research")
            self.assertEqual(blocked.exception.code, 403)
            listed = call(admin, "/api/manage/research")
            self.assertEqual(listed["tasks"][0]["id"], task["id"])
            self.assertTrue(listed["suggestions"])
            payload = {"task_id": task["id"], "version": task["version"],
                       "filename": "protocol.md", "content": "# Synthetic protocol"}
            with self.assertRaises(HTTPError) as blocked:
                call(admin, "/api/manage/research/material", payload)
            self.assertEqual(blocked.exception.code, 403)
            with self.assertRaises(HTTPError) as blocked:
                call(viewer, "/api/manage/research/material", payload, call(viewer, "/api/me")["csrf"])
            self.assertEqual(blocked.exception.code, 403)
            task = call(admin, "/api/manage/research/material", payload, setup["csrf"])
            self.assertEqual(task["status"], "awaiting_assessment")
            material = task["materials"][0]
            with admin.open(base + f"/api/manage/research/material?task={task['id']}&id={material['id']}") as response:
                self.assertEqual(response.read(), payload["content"].encode())
            task = assess(self.root, task["id"], model)
            task = call(admin, "/api/manage/research/decision", {"task_id": task["id"],
                "version": task["version"], "decision": "reject", "note": "Real reproduction is missing"}, setup["csrf"])
            self.assertEqual(task["status"], "rejected")
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)

    def test_user_changes_require_active_admin_and_preserve_last_admin(self):
        from iltkb.management import ReviewStore
        store = ReviewStore(self.root)
        store.add_user("owner", "strong-local-password", "admin")
        admin_id = store.authenticate("owner", "strong-local-password")["id"]
        store.add_user("reader", "another-strong-password", "viewer", admin_id)
        reader_id = store.authenticate("reader", "another-strong-password")["id"]
        with self.assertRaises(PermissionError):
            store.set_user(admin_id, "viewer", True, reader_id)
        with self.assertRaises(ValueError):
            store.set_user(admin_id, "viewer", True, admin_id)
        store.set_user(reader_id, "reviewer", True, admin_id)
        self.assertEqual(store.authenticate("reader", "another-strong-password")["role"], "reviewer")

    def test_quote_realigns_only_with_strong_original_anchor(self):
        source = "- 2. Regularization term R(M): constrains correction plausibility and manufacturability to prevent overcorrection. Mask complexity constraint: limits edge-segment movement.\n"
        incomplete = "Regularization term R(M) constrains correction plausibility and manufacturability to prevent overcorrection."
        self.assertEqual(realign_quote(incomplete, source), source.rstrip("\n"))
        self.assertIsNone(realign_quote("Completely unrelated statement with no reliable match.", source))
        wrapped = "In the pixel-based al-\ngorithm, the source keeps this exact technical statement."
        self.assertEqual(realign_quote("In the pixel-based algorithm, the source keeps this exact technical statement.",
                                       wrapped), wrapped)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        initialize(self.root)
        # Legacy behavior remains covered independently of the review-gated workflow.
        config = read_json(self.root / "iltkb.json")
        config["publication_policy"] = "auto"
        atomic_json(self.root / "iltkb.json", config)
        self.model = ScriptedModel()
        self.write("a.md", "# optimization\nSynthetic condition A: test only, not an actual ILT result.\n")
        self.write("b.md", "# optimization\nSynthetic condition B: differs from A but is not real experimental counter-evidence.\n")

    def tearDown(self):
        self.temp.cleanup()

    def write(self, name, text):
        (self.root / "raw" / name).write_text(text, encoding="utf-8")

    def test_lan_web_requires_https(self):
        from iltkb.webapp import create_server
        with self.assertRaisesRegex(ValueError, "HTTPS is required"):
            create_server(self.root, host="0.0.0.0", port=0)

    def test_end_to_end_graph_and_query(self):
        result = build(self.root, self.model)
        self.assertEqual(result["sources"], 2)
        reader = Reader(self.root)
        self.assertEqual(reader.search("optimization conditions")[0]["topic"], "optimization")
        context = reader.context("optimization conditions")
        self.assertEqual(len(context["evidence"]), 2)
        self.assertEqual(reader.manifest["pages"]["optimization"]["claims"][0]["independence"], "unknown")
        self.assertIn("[E:", reader.answer("optimization", self.model)["answer"])
        self.assertEqual(validate_release(reader.release, self.root / ".iltkb" / "objects"), [])

    def test_local_web_reads_published_release_and_traces_evidence(self):
        from iltkb.management import ReviewStore
        from iltkb.webapp import create_server
        build(self.root, self.model)
        ReviewStore(self.root).add_user("owner", "strong-local-password", "admin")
        server = create_server(self.root, port=0)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        browser = build_opener(HTTPCookieProcessor(CookieJar()))
        try:
            request = Request(base + "/api/login", data=json.dumps({"username": "owner", "password": "strong-local-password"}).encode(),
                              headers={"Content-Type": "application/json"})
            with browser.open(request) as response:
                self.assertEqual(json.load(response)["user"]["role"], "admin")
            with browser.open(base + "/api/overview") as response:
                overview = json.load(response)
            self.assertEqual(overview["stats"]["sources"], 2)
            self.assertEqual(overview["release"], Reader(self.root).release.name)
            with browser.open(base + "/api/topic/optimization") as response:
                topic = json.load(response)
            eid = topic["claims"][0]["evidence"][0]
            with browser.open(base + "/api/evidence/" + eid) as response:
                evidence = json.load(response)
            self.assertEqual(evidence["quote"], Reader(self.root).manifest["evidence"][eid]["quote"])
            sid = evidence["source_id"]
            source = Reader(self.root).manifest["sources"][sid]
            request = Request(base + "/api/source/" + sid + "/original", headers={"Range": "bytes=0-9"})
            with browser.open(request) as response:
                self.assertEqual(response.status, 206)
                self.assertEqual(response.headers["Content-Range"].split("/")[0], "bytes 0-9")
                self.assertEqual(response.read(), (self.root / "raw" / source["path"]).read_bytes()[:10])
            with browser.open(base + "/api/search?q=optimization") as response:
                search = json.load(response)
            self.assertEqual(search["topics"][0]["topic"], "optimization")
            with urlopen(base + "/") as response:
                self.assertIn(b"ILT Atlas", response.read())
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)

    def test_topic_api_reports_zero_based_disagreement_flag(self):
        from iltkb.webapp import create_server

        class ConflictModel(ScriptedModel):
            def complete(self, task, payload):
                result = super().complete(task, payload)
                if task == "synthesize":
                    result["claims"][0]["kind"] = "conflict"
                    result["disagreements"] = [0]
                return result

        build(self.root, ConflictModel())
        ReviewStore(self.root).add_user("owner", "strong-local-password", "admin")
        server = create_server(self.root, port=0)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        browser = build_opener(HTTPCookieProcessor(CookieJar()))
        try:
            request = Request(base + "/api/login", data=json.dumps({"username": "owner", "password": "strong-local-password"}).encode(),
                              headers={"Content-Type": "application/json"})
            with browser.open(request):
                pass
            with browser.open(base + "/api/topic/optimization") as response:
                topic = json.load(response)
            with browser.open(base + "/api/manage/reviews") as response:
                dashboard = json.load(response)
            self.assertEqual(topic["disagreements"], [0])
            self.assertEqual(topic["claims"][0]["auto_flags"], ["conflict", "inconsistent"])
            self.assertEqual(dashboard["items"][0]["auto_flags"], ["conflict", "inconsistent"])
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)

    def test_management_login_roles_review_and_markdown_upload(self):
        from iltkb.webapp import create_server
        build(self.root, self.model)
        server = create_server(self.root, port=0)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        admin = build_opener(HTTPCookieProcessor(CookieJar()))
        viewer = build_opener(HTTPCookieProcessor(CookieJar()))

        def call(opener, path, body=None, csrf=None):
            headers = {"Content-Type": "application/json"} if body is not None else {}
            if csrf is not None:
                headers["X-CSRF-Token"] = csrf
            request = Request(base + path, data=json.dumps(body).encode() if body is not None else None,
                              headers=headers)
            with opener.open(request) as response:
                return json.load(response)

        try:
            self.assertFalse(call(admin, "/api/me")["enabled"])
            self.assertTrue(call(admin, "/api/me")["setup_allowed"])
            with self.assertRaises(HTTPError) as blocked:
                call(admin, "/api/overview")
            self.assertEqual(blocked.exception.code, 401)
            setup = call(admin, "/api/setup", {"username": "owner", "password": "strong-local-password"})
            self.assertEqual(setup["user"]["role"], "admin")
            with self.assertRaises(HTTPError) as blocked:
                call(viewer, "/api/overview")
            self.assertEqual(blocked.exception.code, 401)
            with self.assertRaises(HTTPError) as blocked:
                call(admin, "/api/manage/users", {"username": "reader", "password": "another-strong-password"})
            self.assertEqual(blocked.exception.code, 403)
            call(admin, "/api/manage/users", {"username": "reader", "password": "another-strong-password", "role": "viewer"}, setup["csrf"])
            login = call(viewer, "/api/login", {"username": "reader", "password": "another-strong-password"})
            self.assertEqual(call(viewer, "/api/overview")["stats"]["sources"], 2)
            with self.assertRaises(HTTPError) as blocked:
                call(viewer, "/api/manage/markdown", {"filename": "notes.markdown", "content": "# Test"}, login["csrf"])
            self.assertEqual(blocked.exception.code, 403)
            dashboard = call(admin, "/api/manage/reviews")
            claim_id = dashboard["items"][0]["id"]
            review = call(admin, "/api/manage/review", {"release": dashboard["release"], "claim_id": claim_id,
                                                          "version": 0,
                                                          "decision": "needs_changes", "flags": ["inconsistent"],
                                                          "note": "synthetic review"}, setup["csrf"])
            self.assertEqual(review["decision"], "needs_changes")
            self.assertEqual(review["version"], 1)
            with self.assertRaises(HTTPError) as blocked:
                call(admin, "/api/manage/review", {"release": dashboard["release"], "claim_id": claim_id,
                                                   "version": 0, "decision": "approved", "flags": [], "note": "stale form"}, setup["csrf"])
            self.assertEqual(blocked.exception.code, 409)
            self.assertEqual(call(admin, "/api/manage/reviews")["items"][0]["review"]["flags"], ["inconsistent"])
            call(admin, "/api/manage/review", {"release": dashboard["release"], "claim_id": claim_id,
                                                "version": 1, "decision": "approved", "flags": ["inconsistent"],
                                                "note": "flagged approval remains blocked"}, setup["csrf"])
            self.assertEqual(call(admin, "/api/manage/reviews")["stats"]["approved"], 0)
            call(admin, "/api/manage/markdown", {"filename": "notes.markdown", "content": "# Test\nnew source"}, setup["csrf"])
            self.assertEqual((self.root / "raw" / "notes.markdown").read_text(), "# Test\nnew source")
            self.assertFalse(next(f for f in call(admin, "/api/manage/markdown")["files"] if f["name"] == "notes.markdown")["published"])
            self.assertTrue(call(admin, "/api/manage/audit")["events"])
            build(self.root, self.model)
            after = call(admin, "/api/manage/reviews")
            self.assertNotEqual(after["release"], dashboard["release"])
            self.assertEqual(after["stats"]["stale_reviews"], 1)
            self.assertTrue(next(f for f in call(admin, "/api/manage/markdown")["files"] if f["name"] == "notes.markdown")["published"])
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)

    def test_quarantine_blocks_reads_until_corrected_release_is_reviewed(self):
        from iltkb.incidents import impact, quarantine, resolve
        from iltkb.management import ReviewStore
        from iltkb.webapp import create_server
        build(self.root, self.model)
        before = Reader(self.root)
        sid = next(sid for sid, source in before.manifest["sources"].items() if source["path"] == "a.md")
        report = impact(self.root, sid)
        self.assertEqual([topic["id"] for topic in report["topics"]], ["optimization"])
        self.assertEqual(report["topics"][0]["claims_at_risk"], 1)
        self.assertEqual(len(report["related_sources_to_check"]), 1)
        quarantine(self.root, sid, "Source contains a false statement")
        with self.assertRaises(ValueError):
            before.search("optimization")
        with self.assertRaises(ValueError):
            Reader(self.root)
        with self.assertRaises(ValueError):
            resolve(self.root, sid, "Reviewed clean release")
        server = create_server(self.root, port=0)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        browser = build_opener(HTTPCookieProcessor(CookieJar()))
        try:
            request = Request(base + "/api/setup", data=json.dumps({"username": "owner", "password": "strong-local-password"}).encode(),
                              headers={"Content-Type": "application/json"})
            with browser.open(request) as response:
                setup = json.load(response)
            with self.assertRaises(HTTPError) as blocked:
                browser.open(base + "/api/overview")
            self.assertEqual(blocked.exception.code, 423)
            with browser.open(base + "/api/manage/impact?source_id=" + sid) as response:
                self.assertEqual(json.load(response)["source_id"], sid)
            build(self.root, self.model)
            current = current_release(self.root)
            manifest = read_json(current / "manifest.json")
            self.assertFalse(any(row["source_id"] == sid for row in manifest["evidence"].values()))
            with self.assertRaises(HTTPError) as blocked:
                browser.open(base + "/api/overview")
            self.assertEqual(blocked.exception.code, 423)
            with self.assertRaises(ValueError):
                resolve(self.root, sid, "Reviewed clean topic")
            atomic_json(self.root / "raw" / "a.md.meta.json", {"status": "withdrawn"})
            with self.assertRaises(ValueError):
                resolve(self.root, sid, "Reviewed clean topic")
            store = ReviewStore(self.root)
            admin_id = store.authenticate("owner", "strong-local-password")["id"]
            for claim in manifest["pages"]["optimization"]["claims"]:
                store.save_review(current.name, claim, "approved", [], "Reviewed against remaining source", admin_id, 0)
            request = Request(base + "/api/manage/resolve", data=json.dumps({"source_id": sid, "reason": "Reviewed clean topic"}).encode(),
                              headers={"Content-Type": "application/json", "X-CSRF-Token": setup["csrf"]})
            with browser.open(request) as response:
                self.assertEqual(json.load(response)["resolved_release"], current.name)
            with browser.open(base + "/api/overview") as response:
                self.assertEqual(json.load(response)["release"], current.name)
            self.assertTrue(Reader(self.root).search("optimization"))
            build(self.root, self.model)
            self.assertFalse(any(row["source_id"] == sid for row in Reader(self.root).manifest["evidence"].values()))
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)

    def test_corrected_source_can_replace_quarantined_version(self):
        from iltkb.incidents import quarantine, resolve
        from iltkb.management import ReviewStore
        build(self.root, self.model)
        first = Reader(self.root)
        sid = next(sid for sid, source in first.manifest["sources"].items() if source["path"] == "a.md")
        old_hash = first.manifest["sources"][sid]["sha256"]
        quarantine(self.root, sid, "Incorrect source statement")
        self.write("a.md", "# optimization\nCondition A was corrected; this is synthetic test text.\n")
        build(self.root, self.model)
        with self.assertRaises(ValueError):
            Reader(self.root)
        corrected = read_json(current_release(self.root) / "manifest.json")
        self.assertNotEqual(corrected["sources"][sid]["sha256"], old_hash)
        self.assertEqual(corrected["sources"][sid]["status"], "active")
        self.assertFalse(any(row["source_hash"] == old_hash for row in corrected["evidence"].values()))
        with self.assertRaises(ValueError):
            resolve(self.root, sid, "Corrected source and reviewed topic")
        store = ReviewStore(self.root)
        store.add_user("owner", "strong-local-password", "admin")
        admin_id = store.authenticate("owner", "strong-local-password")["id"]
        claims = corrected["pages"]["optimization"]["claims"]
        for index, claim in enumerate(claims):
            store.save_review(current_release(self.root).name, claim, "approved",
                              ["stale"] if index == 0 else [], "Reviewed correction", admin_id, 0)
        with self.assertRaisesRegex(ValueError, "Approve every current claim"):
            resolve(self.root, sid, "Flagged approval cannot release quarantine")
        store.save_review(current_release(self.root).name, claims[0], "approved", [],
                          "Flag cleared after review", admin_id, 1)
        resolve(self.root, sid, "Corrected source and reviewed topic")
        self.assertTrue(any(row["source_id"] == sid for row in Reader(self.root).manifest["evidence"].values()))

    def test_answer_retries_combined_citation_bracket(self):
        class FixesCitation(ScriptedModel):
            def complete(self, task, payload):
                if task == "answer" and self.calls.count("answer") == 0:
                    self.calls.append(task)
                    eid = next(iter(payload["evidence"]))
                    return {"answer": f"Synthetic test conclusion.[E:{eid}, E:{eid}]", "evidence": [eid]}
                return super().complete(task, payload)

        model = FixesCitation()
        build(self.root, model)
        model.calls.clear()
        result = Reader(self.root).answer("optimization", model)
        self.assertEqual(model.calls.count("answer"), 2)
        self.assertEqual(len(result["evidence"]), 1)

    def test_no_change_makes_no_model_calls(self):
        build(self.root, self.model)
        self.model.calls.clear()
        result = build(self.root, self.model)
        self.assertEqual(self.model.calls, [])
        self.assertEqual(result["rebuilt_topics"], [])

    def test_new_counterevidence_rebuilds_existing_topic(self):
        build(self.root, self.model)
        self.model.calls.clear()
        self.write("counter.md", "# optimization\nSynthetic counterexample testing synthesis after adding a source.\n")
        result = build(self.root, self.model)
        self.assertEqual(result["rebuilt_topics"], ["optimization"])
        self.assertEqual(self.model.calls.count("extract"), 1)
        self.assertEqual(self.model.calls.count("synthesize"), 1)
        self.assertEqual(len(Reader(self.root).manifest["pages"]["optimization"]["considered_evidence"]), 3)

    def test_markdown_extension_is_compiled(self):
        self.write("additional.markdown", "# optimization\nSynthetic Markdown extension input for scanning tests.\n")
        result = build(self.root, self.model)
        self.assertEqual(result["sources"], 3)
        self.assertIn("additional.markdown", {source["path"] for source in Reader(self.root).manifest["sources"].values()})

    def test_delete_removes_old_claim_evidence(self):
        build(self.root, self.model)
        before = Reader(self.root)
        deleted_id = next(k for k, s in before.manifest["sources"].items() if s["path"] == "a.md")
        (self.root / "raw" / "a.md").unlink()
        build(self.root, self.model)
        after = Reader(self.root)
        self.assertNotIn(deleted_id, after.manifest["sources"])
        self.assertFalse(any(e["source_id"] == deleted_id for e in after.manifest["evidence"].values()))
        (self.root / "raw" / "b.md").unlink()
        build(self.root, self.model)
        self.assertEqual(Reader(self.root).manifest["pages"]["optimization"]["claims"], [])

    def test_modification_and_rule_revision_invalidation(self):
        build(self.root, self.model)
        old = Reader(self.root)
        sid = next(k for k, v in old.manifest["sources"].items() if v["path"] == "a.md")
        self.write("a.md", "# Changed\nCondition changed to test cache invalidation.\n")
        self.model.calls.clear()
        build(self.root, self.model)
        new = Reader(self.root)
        self.assertIn(sid, new.manifest["sources"])
        self.assertNotEqual(old.manifest["sources"][sid]["sha256"], new.manifest["sources"][sid]["sha256"])
        self.assertEqual(self.model.calls.count("extract"), 1)
        self.model.fingerprint = {"model": "synthetic-test-only", "revision": "2"}
        self.model.calls.clear()
        build(self.root, self.model)
        self.assertEqual(self.model.calls.count("extract"), 2)

    def test_rename_preserves_source_id_and_pinned_reader(self):
        build(self.root, self.model)
        pinned = Reader(self.root)
        old_sid = next(k for k, v in pinned.manifest["sources"].items() if v["path"] == "a.md")
        (self.root / "raw" / "a.md").rename(self.root / "raw" / "renamed.md")
        self.model.calls.clear()
        build(self.root, self.model)
        new = Reader(self.root)
        self.assertEqual(new.manifest["sources"][old_sid]["path"], "renamed.md")
        self.assertEqual(pinned.manifest["sources"][old_sid]["path"], "a.md")
        self.assertNotEqual(pinned.release, new.release)
        self.assertEqual(self.model.calls.count("extract"), 0)

    def test_failed_candidates_never_replace_current(self):
        build(self.root, self.model)
        previous = current_release(self.root)
        self.write("a.md", "# Updated\nChange source to trigger a rebuild.")
        for failure in ("quote", "circular", "omission", "review"):
            with self.subTest(failure=failure):
                self.model.fail = failure
                with self.assertRaises(ValueError):
                    build(self.root, self.model)
                self.assertEqual(current_release(self.root), previous)
                self.assertFalse((self.root / ".iltkb" / "writer.lock").exists())

    def test_writer_exclusion(self):
        with writer_lock(self.root / ".iltkb"):
            with self.assertRaisesRegex(ValueError, "Writer lock"):
                build(self.root, self.model)

    def test_generated_page_cannot_be_reingested(self):
        build(self.root, self.model)
        reader = Reader(self.root)
        page = reader.release / "wiki" / "methods" / "optimization.md"
        self.write("copied-wiki.md", page.read_text(encoding="utf-8"))
        with self.assertRaisesRegex(ValueError, "re-ingested"):
            build(self.root, self.model)
        self.assertEqual(current_release(self.root), reader.release)

    def test_modified_generated_artifact_is_detected(self):
        build(self.root, self.model)
        reader = Reader(self.root)
        page = reader.release / "wiki" / "methods" / "optimization.md"
        page.write_text("Altered knowledge page")
        with self.assertRaisesRegex(ValueError, "artifact hash mismatch"):
            validate_release(reader.release, self.root / ".iltkb" / "objects")

    def test_missing_or_corrupt_snapshot_detected(self):
        build(self.root, self.model)
        reader = Reader(self.root)
        source = next(iter(reader.manifest["sources"].values()))
        (self.root / ".iltkb" / "objects" / source["sha256"]).write_text("corrupted")
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            validate_release(reader.release, self.root / ".iltkb" / "objects")

    def test_metadata_independence_change_rebuilds_without_extraction(self):
        build(self.root, self.model)
        for name in ("a.md", "b.md"):
            atomic_json(self.root / "raw" / (name + ".meta.json"), {"origin_group": "same-experiment"})
        self.model.calls.clear()
        build(self.root, self.model)
        claim = Reader(self.root).manifest["pages"]["optimization"]["claims"][0]
        self.assertEqual(claim["independence"], "shared_origin")
        self.assertEqual(self.model.calls.count("extract"), 0)
        self.assertEqual(self.model.calls.count("synthesize"), 1)

    def test_one_declared_origin_is_still_one_source(self):
        (self.root / "raw" / "b.md").unlink()
        atomic_json(self.root / "raw" / "a.md.meta.json", {"origin_group": "one-course"})
        build(self.root, self.model)
        claim = Reader(self.root).manifest["pages"]["optimization"]["claims"][0]
        self.assertEqual(claim["independence"], "single_source")

    def test_unknown_answer_citation_rejected(self):
        build(self.root, self.model)
        self.model.fail = "answer"
        with self.assertRaisesRegex(ValueError, "citations"):
            Reader(self.root).answer("optimization", self.model)

    def test_withdrawal_and_restoration(self):
        build(self.root, self.model)
        atomic_json(self.root / "raw" / "a.md.meta.json", {"status": "withdrawn"})
        build(self.root, self.model)
        reader = Reader(self.root)
        sid = next(k for k, v in reader.manifest["sources"].items() if v["path"] == "a.md")
        self.assertFalse(any(e["source_id"] == sid for e in reader.manifest["evidence"].values()))
        atomic_json(self.root / "raw" / "a.md.meta.json", {"status": "active"})
        build(self.root, self.model)
        self.assertTrue(any(e["source_id"] == sid for e in Reader(self.root).manifest["evidence"].values()))

    def test_unchanged_sections_reuse_reviewed_extraction(self):
        self.write("a.md", "# A\nStable paragraph.\n# B\nBefore the change.\n")
        build(self.root, self.model)
        self.write("a.md", "# A\nStable paragraph.\n# B\nAfter the change.\n")
        self.model.calls.clear()
        build(self.root, self.model)
        self.assertEqual(self.model.calls.count("extract"), 1)

    def test_agreement_requires_multiple_sources(self):
        from iltkb.engine import validate_candidate
        evidence = {"E1": {"source_id": "S1"}}
        candidate = self.model.complete("synthesize", {"evidence": evidence})
        candidate["agreements"] = [0]
        with self.assertRaisesRegex(ValueError, "multiple sources"):
            validate_candidate(candidate, evidence)
        candidate["agreements"] = [{"claim_indexes": [0]}]
        with self.assertRaisesRegex(ValueError, "Invalid comparison indexes"):
            validate_candidate(candidate, evidence)

    def test_invalid_agreement_is_removed_before_publication(self):
        class RepairsAgreement(ScriptedModel):
            def complete(self, task, payload):
                result = super().complete(task, payload)
                if task == "synthesize" and self.calls.count("synthesize") == 1:
                    result["claims"][0]["evidence"] = result["claims"][0]["evidence"][:1]
                    result["agreements"] = [0]
                return result

        model = RepairsAgreement()
        build(self.root, model)
        self.assertEqual(model.calls.count("synthesize"), 1)
        self.assertEqual(Reader(self.root).manifest["pages"]["optimization"]["agreements"], [])
        self.assertEqual(validate_release(Reader(self.root).release,
                                          self.root / ".iltkb" / "objects"), [])

    def test_parallel_extraction_and_serial_publication(self):
        config = read_json(self.root / "iltkb.json")
        config["workers"] = 2
        atomic_json(self.root / "iltkb.json", config)
        build(self.root, self.model)
        reader = Reader(self.root)
        self.assertEqual(len(reader.manifest["evidence"]), 2)
        validate_release(reader.release, self.root / ".iltkb" / "objects")

    def test_markdown_visual_pending(self):
        self.write("image.md", "# conditions\n![test figure](figure.png)\n[SVG illustration 1: visual content pending]\n")
        build(self.root, self.model)
        reader = Reader(self.root)
        source = next(v for v in reader.manifest["sources"].values() if v["path"] == "image.md")
        self.assertEqual(len(source["visual_pending"]), 2)
        self.assertIn("Items pending visual review", (reader.release / "coverage.md").read_text())

    def test_imported_visual_markers_remain_readable(self):
        self.write("visuals.md", "[SVG illustration 1: visual content pending]\n"
                   "[SVG \u56fe\u793a 2\uff1a\u89c6\u89c9\u5185\u5bb9\u5f85\u5904\u7406]\n")
        _, _, _, pending = load_document(self.root / "raw" / "visuals.md")
        self.assertEqual(len(pending), 2)

    def test_chunks_preserve_all_characters_including_long_lines(self):
        text = "# A\n" + "\u4e2d\u6587" * 50 + "\n```\n# not-heading\n```\n## B\nlast\n"
        parts = chunks(text, 13)
        self.assertEqual("".join(p["text"] for p in parts), text)
        self.assertEqual(parts[0]["start"], 0)
        self.assertEqual(parts[-1]["end"], len(text))
        self.assertFalse(any("not-heading" in p["section"] for p in parts))
        self.assertTrue(tokens("\u5149\u523b\u4f18\u5316\u65b9\u6cd5") & tokens("\u4f18\u5316"))

    def test_coalesced_sections_keep_exact_positions(self):
        text = "# A\nFirst paragraph.\n## B\nSecond paragraph.\n"
        parts = chunks(text, 100, coalesce_sections=True)
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0]["text"], text)
        self.assertEqual([row["section"] for row in parts[0]["sections"]], ["A", "A / B"])
        config = read_json(self.root / "iltkb.json")
        config["coalesce_sections"] = True
        atomic_json(self.root / "iltkb.json", config)
        self.write("a.md", text)
        build(self.root, self.model)
        reader = Reader(self.root)
        rows = [e for e in reader.manifest["evidence"].values() if e["quote"] == text]
        self.assertEqual(rows[0]["section"], "A")

    def test_pdf_text_and_page_pending(self):
        try:
            import pymupdf
        except ImportError:
            self.skipTest("Optional PDF parser not installed")
        path = self.root / "raw" / "test.pdf"
        with pymupdf.open() as doc:
            page = doc.new_page()
            page.insert_text((72, 72), "Synthetic ILT fixture, not scientific evidence.")
            doc.save(path)
        original, text, pages, pending = load_document(path)
        self.assertIn("Synthetic ILT", text)
        self.assertEqual(pages[0]["page"], 1)
        self.assertEqual(pending[0]["page"], 1)
        build(self.root, self.model)
        reader = Reader(self.root)
        evidence = [e for e in reader.manifest["evidence"].values() if e["page"] is not None]
        self.assertEqual(evidence[0]["page"], 1)

    def test_pdf_selected_pages_keep_original_page_numbers(self):
        try:
            import pymupdf
        except ImportError:
            self.skipTest("Optional PDF parser not installed")
        path = self.root / "raw" / "selected.pdf"
        with pymupdf.open() as doc:
            for number in range(1, 4):
                page = doc.new_page()
                page.insert_text((72, 72), f"Synthetic ILT page {number}, not scientific evidence.")
            doc.save(path)
        atomic_json(path.with_suffix(".pdf.meta.json"), {"source_id": "selected-pdf", "pdf_pages": [1, 3]})
        _, text, pages, pending = load_document(path, [1, 3])
        self.assertIn("page 1", text)
        self.assertNotIn("page 2", text)
        self.assertEqual([p["page"] for p in pages], [1, 3])
        self.assertEqual([p["page"] for p in pending], [1, 3])
        build(self.root, self.model)
        reader = Reader(self.root)
        self.assertEqual(reader.manifest["sources"]["selected-pdf"]["pdf_pages_selected"], [1, 3])
        self.assertEqual(validate_release(reader.release, self.root / ".iltkb" / "objects"), ["selected.pdf: visuals pending"])

    def test_real_langchain_ollama_adapter_uses_messages(self):
        import importlib.util
        if importlib.util.find_spec("langchain_ollama") is None:
            self.skipTest("Optional Ollama adapter not installed")
        from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
        class Chat:
            def invoke(self, messages):
                self.messages = messages
                return AIMessage(content='{"coverage_ok":true,"issues":[]}', response_metadata={"done_reason": "stop"})
        model = OllamaModel(model="not-called", max_input_chars=1000)
        model.chat = Chat()
        result = model.complete("extract_review", {"text": "test"})
        self.assertTrue(result["coverage_ok"])
        self.assertIsInstance(model.chat.messages[0], SystemMessage)
        self.assertIsInstance(model.chat.messages[1], HumanMessage)
        with self.assertRaisesRegex(ValueError, "budget"):
            model.complete("extract", {"text": "x" * 2000})


class PublicationGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        initialize(self.root)
        (self.root / "raw" / "a.md").write_text("# optimization\nSynthetic condition A for testing only.\n", encoding="utf-8")
        self.model = ScriptedModel()
        self.store = ReviewStore(self.root)
        self.store.add_user("owner", "strong-local-password", "admin")
        self.admin = self.store.authenticate("owner", "strong-local-password")["id"]

    def tearDown(self):
        self.temp.cleanup()

    def approve(self, report):
        manifest = read_json(self.root / ".iltkb" / "releases" / report["release"] / "manifest.json")
        for page in manifest["pages"].values():
            for claim in page["claims"]:
                self.store.save_review(report["release"], claim, "approved", [], "Checked against synthetic source",
                                       self.admin, 0)

    def test_first_build_requires_candidate_review_and_explicit_promotion(self):
        result = build(self.root, self.model)
        self.assertEqual(result["status"], "pending_review")
        self.assertIsNone(current_release(self.root))
        with self.assertRaises(ValueError):
            Reader(self.root)
        report = candidate_report(self.root)
        self.assertFalse(report["ready"])
        self.assertTrue(report["items"][0]["evidence_details"][0]["quote"])
        with self.assertRaisesRegex(ValueError, "requiring approval"):
            promote_candidate(self.root, self.admin)
        with self.assertRaisesRegex(ValueError, "awaits review"):
            build(self.root, self.model)
        self.approve(report)
        self.assertTrue(candidate_report(self.root)["ready"])
        promoted = promote_candidate(self.root, self.admin)
        self.assertEqual(current_release(self.root).name, promoted["release"])
        self.assertIsNone(candidate_report(self.root))
        self.assertTrue(validate_release(current_release(self.root), self.root / ".iltkb" / "objects") == [])

    def test_carry_forward_and_candidate_rejection_override(self):
        first = build(self.root, self.model)
        self.approve(candidate_report(self.root))
        promote_candidate(self.root, self.admin)
        second = build(self.root, self.model)
        self.assertNotEqual(first["release"], second["release"])
        report = candidate_report(self.root)
        self.assertTrue(report["ready"])
        self.assertEqual(report["stats"]["carried"], report["stats"]["total"])
        manifest = read_json(self.root / ".iltkb" / "releases" / second["release"] / "manifest.json")
        claim = next(claim for page in manifest["pages"].values() for claim in page["claims"])
        self.store.save_review(second["release"], claim, "rejected", [], "Found issue", self.admin, 0)
        self.assertFalse(candidate_report(self.root)["ready"])
        with self.assertRaises(ValueError):
            promote_candidate(self.root, self.admin)
        discard_candidate(self.root, self.admin, "Rebuild after correction")
        (self.root / "raw" / "b.md").write_text("# optimization\nSynthetic condition B for testing only.\n", encoding="utf-8")
        build(self.root, self.model)
        changed = candidate_report(self.root)
        self.assertFalse(changed["ready"])
        self.assertGreater(changed["stats"]["changed_topics"], 0)
        self.assertEqual(current_release(self.root).name, first["release"])

    def test_candidate_http_bootstrap_and_role_gate(self):
        from iltkb.webapp import create_server
        build(self.root, self.model)
        self.store.add_user("reviewer", "reviewer-password", "reviewer", self.admin)
        self.store.add_user("reader", "reader-password", "viewer", self.admin)
        server = create_server(self.root, port=0)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        admin = build_opener(HTTPCookieProcessor(CookieJar()))
        reviewer = build_opener(HTTPCookieProcessor(CookieJar()))
        viewer = build_opener(HTTPCookieProcessor(CookieJar()))

        def call(opener, path, body=None, csrf=None):
            headers = {"Content-Type": "application/json"} if body is not None else {}
            if csrf is not None:
                headers["X-CSRF-Token"] = csrf
            request = Request(base + path, data=json.dumps(body).encode() if body is not None else None,
                              headers=headers)
            with opener.open(request) as response:
                return json.load(response)

        try:
            self.assertFalse(call(admin, "/api/me")["has_release"])
            self.assertTrue(call(admin, "/api/me")["has_candidate"])
            admin_auth = call(admin, "/api/login", {"username": "owner", "password": "strong-local-password"})
            with self.assertRaises(HTTPError) as blocked:
                call(admin, "/api/overview")
            self.assertEqual(blocked.exception.code, 409)
            reviewer_auth = call(reviewer, "/api/login", {"username": "reviewer", "password": "reviewer-password"})
            call(viewer, "/api/login", {"username": "reader", "password": "reader-password"})
            with self.assertRaises(HTTPError) as blocked:
                call(viewer, "/api/manage/candidate")
            self.assertEqual(blocked.exception.code, 403)
            candidate = call(reviewer, "/api/manage/candidate")["candidate"]
            self.assertFalse(candidate["ready"])
            with self.assertRaises(HTTPError) as blocked:
                call(reviewer, "/api/manage/candidate/publish", {}, reviewer_auth["csrf"])
            self.assertEqual(blocked.exception.code, 403)
            with self.assertRaises(HTTPError) as blocked:
                call(admin, "/api/manage/candidate/publish", {}, admin_auth["csrf"])
            self.assertEqual(blocked.exception.code, 400)
            for item in candidate["items"]:
                call(reviewer, "/api/manage/review", {"release": candidate["release"], "claim_id": item["id"],
                     "decision": "approved", "flags": [], "note": "Synthetic evidence checked", "version": 0},
                     reviewer_auth["csrf"])
            self.assertTrue(call(admin, "/api/manage/candidate")["candidate"]["ready"])
            with self.assertRaises(HTTPError) as blocked:
                call(admin, "/api/manage/candidate/publish", {})
            self.assertEqual(blocked.exception.code, 403)
            call(admin, "/api/manage/candidate/publish", {}, admin_auth["csrf"])
            self.assertTrue(call(viewer, "/api/me")["has_release"])
            self.assertEqual(call(viewer, "/api/overview")["release"], candidate["release"])
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)

    def test_candidate_manifest_tamper_blocks_promotion(self):
        result = build(self.root, self.model)
        report = candidate_report(self.root)
        self.approve(report)
        manifest_path = self.root / ".iltkb" / "releases" / result["release"] / "manifest.json"
        manifest = read_json(manifest_path)
        page = next(page for page in manifest["pages"].values() if page["claims"])
        page["claims"][0]["reasoning"] = "Changed after approval"
        atomic_json(manifest_path, manifest)
        with self.assertRaisesRegex(ValueError, "manifest hash mismatch"):
            promote_candidate(self.root, self.admin)
        self.assertIsNone(current_release(self.root))


if __name__ == "__main__":
    unittest.main()
