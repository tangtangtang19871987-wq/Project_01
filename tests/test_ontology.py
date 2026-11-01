"""Synthetic ontology tests; no model service or scientific claim is used."""
from __future__ import annotations

import copy
from http.cookiejar import CookieJar
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import build_opener, HTTPCookieProcessor, Request

from iltkb.engine import build, initialize
from iltkb.management import ReviewStore
from iltkb.ontology import OntologyStore, _graph, export_ntriples, load_schema, published_graph
from iltkb.publication import candidate_report, promote_candidate
from iltkb.storage import atomic_json, current_release, read_json


class TwoClaimModel:
    fingerprint = {"model": "synthetic-ontology-test", "revision": "1"}

    def complete(self, task, payload):
        if task == "extract":
            return {"items": [{"quote": payload["text"], "statement": payload["text"],
                               "topics": ["optimization"], "conditions": {"scope": "synthetic"}}]}
        if task == "extract_review":
            return {"coverage_ok": True, "issues": []}
        if task == "synthesize":
            refs = sorted(payload["evidence"])
            return {"summary": "Synthetic relations only", "keywords": ["synthetic"],
                    "claims": [{"kind": "inference", "text": f"Synthetic statement {index}",
                                "evidence": [eid], "conditions": {"scope": "synthetic"},
                                "reasoning": "Fixture for relation review only"}
                               for index, eid in enumerate(refs)],
                    "agreements": [], "disagreements": [], "gaps": ["Scientific verification pending"],
                    "considered_evidence": refs}
        if task == "review":
            return {"coverage_ok": True, "issues": [],
                    "claims": [{"index": index, "supported": True, "reason": "Synthetic fixture"}
                               for index in range(len(payload["candidate"]["claims"]))]}
        raise AssertionError(task)


class OntologyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        initialize(self.root)
        (self.root / "raw" / "a.md").write_text("# optimization\nSynthetic condition A for testing only.\n", encoding="utf-8")
        (self.root / "raw" / "b.md").write_text("# optimization\nSynthetic condition B for testing only.\n", encoding="utf-8")
        self.model = TwoClaimModel()
        self.reviews = ReviewStore(self.root)
        self.reviews.add_user("owner", "strong-local-password", "admin")
        self.admin = self.reviews.authenticate("owner", "strong-local-password")["id"]
        self.reviews.add_user("reviewer", "reviewer-password", "reviewer", self.admin)
        self.reviewer = self.reviews.authenticate("reviewer", "reviewer-password")["id"]
        build(self.root, self.model)
        report = candidate_report(self.root)
        manifest = read_json(self.root / ".iltkb" / "releases" / report["release"] / "manifest.json")
        for page in manifest["pages"].values():
            for claim in page["claims"]:
                self.reviews.save_review(report["release"], claim, "approved", [], "Synthetic text checked",
                                         self.reviewer, 0)
        promote_candidate(self.root, self.admin)
        self.manifest = read_json(current_release(self.root) / "manifest.json")
        self.claims = next(page["claims"] for page in self.manifest["pages"].values() if len(page["claims"]) == 2)
        self.store = OntologyStore(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def relation(self):
        first, second = self.claims
        return ("claim:" + first["id"], "dependsOn", "claim:" + second["id"],
                [first["evidence"][0], second["evidence"][0]])

    def test_reviewed_relation_is_separately_published_with_explanation(self):
        self.assertEqual(published_graph(self.root)["status"], "unpublished")
        subject, predicate, object_id, evidence = self.relation()
        with self.assertRaisesRegex(ValueError, "each claim endpoint"):
            self.store.propose(self.reviewer, subject, predicate, object_id, evidence[:1], "Synthetic rationale")
        proposal = self.store.propose(self.reviewer, subject, predicate, object_id, evidence, "Synthetic rationale")
        self.assertEqual(published_graph(self.root)["status"], "unpublished")
        with self.assertRaisesRegex(ValueError, "cannot approve their own"):
            self.store.decide(self.reviewer, proposal["id"], "approved", "Self review should fail", proposal["version"])
        self.store.decide(self.admin, proposal["id"], "approved", "Synthetic review only", proposal["version"])
        self.assertEqual(published_graph(self.root)["status"], "unpublished")
        self.store.publish(self.admin)
        published = published_graph(self.root)
        self.assertEqual(published["status"], "published")
        facts = published["graph"]["facts"]
        direct = next(fact for fact in facts if fact["subject"] == subject and
                      fact["predicate"] == "dependsOn" and fact["object"] == object_id)
        derived = next(fact for fact in facts if fact["subject"] == subject and
                       fact["predicate"] == "relatedTo" and fact["object"] == object_id)
        self.assertEqual(direct["rule"], "approved_relation")
        self.assertEqual(direct["evidence_ids"], evidence)
        self.assertEqual(derived["rule"], "rdfs:subPropertyOf")
        self.assertEqual(derived["premises"], [direct["id"]])
        self.assertFalse(published["graph"]["issues"])

    def test_retracted_claim_approval_hides_published_semantics(self):
        subject, predicate, object_id, evidence = self.relation()
        proposal = self.store.propose(self.reviewer, subject, predicate, object_id, evidence, "Synthetic rationale")
        self.store.decide(self.admin, proposal["id"], "approved", "Synthetic review only", 1)
        self.store.publish(self.admin)
        self.reviews.save_review(current_release(self.root).name, self.claims[0], "rejected", [],
                                 "Retraction test", self.reviewer, 1)
        status = published_graph(self.root)
        self.assertEqual(status["status"], "stale")
        self.assertNotIn("graph", status)
        self.assertFalse(self.store.report()["ready"])

    def test_rdf_export_requires_current_snapshot_and_keeps_derivation(self):
        with self.assertRaisesRegex(ValueError, "current ontology snapshot"):
            export_ntriples(self.root)
        subject, predicate, object_id, evidence = self.relation()
        proposal = self.store.propose(self.reviewer, subject, predicate, object_id, evidence,
                                      "Synthetic relation for RDF export")
        self.store.decide(self.admin, proposal["id"], "approved", "Synthetic review only", 1)
        self.store.publish(self.admin)
        triples = export_ntriples(self.root)
        self.assertEqual(triples, export_ntriples(self.root))
        self.assertIn("<http://www.w3.org/2000/01/rdf-schema#subPropertyOf>", triples)
        self.assertIn("<http://www.w3.org/ns/prov#wasDerivedFrom>", triples)
        self.assertIn('"rdfs:subPropertyOf"', triples)
        self.assertTrue(all(line.startswith("<urn:uuid:") and line.endswith(" .")
                            for line in triples.splitlines()))
        self.reviews.save_review(current_release(self.root).name, self.claims[0], "rejected", [],
                                 "RDF invalidation test", self.reviewer, 1)
        with self.assertRaisesRegex(ValueError, "current ontology snapshot"):
            export_ntriples(self.root)

    def test_schema_change_and_release_change_invalidate_semantics(self):
        self.store.publish(self.admin)  # Structural graph only.
        schema_path = self.root / "schema" / "ontology.json"
        schema = read_json(schema_path)
        schema["entities"]["ILT"] = {"label": "Inverse lithography technology", "class": "Method"}
        atomic_json(schema_path, schema)
        self.assertEqual(published_graph(self.root)["status"], "stale")
        self.store.publish(self.admin)
        self.assertEqual(published_graph(self.root)["status"], "published")
        old_release = current_release(self.root).name
        build(self.root, self.model)
        report = candidate_report(self.root)
        self.assertTrue(report["ready"])
        self.assertEqual(report["stats"]["carried"], report["stats"]["total"])
        promote_candidate(self.root, self.admin)
        self.assertNotEqual(current_release(self.root).name, old_release)
        copied = self.reviews.reviews(current_release(self.root).name)
        self.assertTrue(all(row["decision"] == "approved" and row["carried_from"] == old_release
                            for row in copied.values()))
        self.assertEqual(published_graph(self.root)["status"], "stale")

    def test_schema_validation_and_owl_disjoint_issue(self):
        schema, _ = load_schema(self.root)
        bad = copy.deepcopy(schema)
        bad["classes"]["Claim"]["parent"] = "Inference"
        atomic_json(self.root / "schema" / "ontology.json", bad)
        with self.assertRaisesRegex(ValueError, "cycle"):
            load_schema(self.root)
        atomic_json(self.root / "schema" / "ontology.json", schema)
        first, second = self.relation()[0:3:2]
        graph = _graph(self.manifest, schema,
                       [{"id": "test-only", "subject": first, "predicate": "hasEvidence",
                         "object": second, "evidence_ids": []}])
        self.assertTrue(any(issue["kind"] == "owl:disjointWith" for issue in graph["issues"]))

    def test_http_roles_csrf_and_publication_boundary(self):
        from iltkb.webapp import create_server
        self.reviews.add_user("reader", "reader-password", "viewer", self.admin)
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
            admin_auth = call(admin, "/api/login", {"username": "owner", "password": "strong-local-password"})
            reviewer_auth = call(reviewer, "/api/login", {"username": "reviewer", "password": "reviewer-password"})
            call(viewer, "/api/login", {"username": "reader", "password": "reader-password"})
            self.assertEqual(call(viewer, "/api/ontology")["status"], "unpublished")
            with self.assertRaises(HTTPError) as blocked:
                viewer.open(base + "/api/ontology.nt")
            self.assertEqual(blocked.exception.code, 409)
            with self.assertRaises(HTTPError) as blocked:
                call(viewer, "/api/manage/ontology")
            self.assertEqual(blocked.exception.code, 403)
            self.assertGreater(call(reviewer, "/api/manage/ontology")["preview"]["stats"]["nodes"], 0)
            subject, predicate, object_id, evidence = self.relation()
            body = {"subject": subject, "predicate": predicate, "object": object_id,
                    "evidence_ids": evidence, "note": "Synthetic relation for HTTP test"}
            with self.assertRaises(HTTPError) as blocked:
                call(reviewer, "/api/manage/ontology/propose", body)
            self.assertEqual(blocked.exception.code, 403)
            proposal = call(reviewer, "/api/manage/ontology/propose", body, reviewer_auth["csrf"])
            call(admin, "/api/manage/ontology/decision",
                 {"id": proposal["id"], "decision": "approved", "note": "Synthetic review", "version": 1},
                 admin_auth["csrf"])
            self.assertEqual(call(viewer, "/api/ontology")["status"], "unpublished")
            with self.assertRaises(HTTPError) as blocked:
                call(reviewer, "/api/manage/ontology/publish", {}, reviewer_auth["csrf"])
            self.assertEqual(blocked.exception.code, 403)
            call(admin, "/api/manage/ontology/publish", {}, admin_auth["csrf"])
            self.assertEqual(call(viewer, "/api/ontology")["status"], "published")
            with viewer.open(base + "/api/ontology.nt") as response:
                self.assertEqual(response.headers.get_content_type(), "application/n-triples")
                self.assertIn(b"prov#wasDerivedFrom", response.read())
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
