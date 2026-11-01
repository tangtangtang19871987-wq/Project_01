from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .engine import build, initialize, validate_release
from .model import GeminiModel, OllamaModel
from .retrieval import Reader
from .storage import current_release, read_json


def configured_model(root):
    config = read_json(root / "iltkb.json")
    provider = config.get("provider", "ollama")  # Backward compatible with first release.
    if provider not in ("gemini", "ollama"):
        raise ValueError("provider must be gemini or ollama")
    if not config.get("model"):
        raise ValueError(f"Set model in iltkb.json to an available {provider} model ID")
    if not config.get("model_revision") or config["model_revision"] == "set-after-selecting-model":
        raise ValueError("Set model_revision to the model digest or an immutable revision label; cache invalidation requires it")
    if provider == "gemini":
        env_file = config.get("env_file")
        if env_file is not None:
            if not isinstance(env_file, str) or not env_file.strip():
                raise ValueError("env_file must be a nonempty path")
            env_file = Path(env_file).expanduser()
            if not env_file.is_absolute():
                env_file = root / env_file
        return GeminiModel(model=config["model"], key_env=config.get("api_key_env", "GEMINI_API_KEY"),
                           env_file=env_file,
                           max_input_chars=config.get("max_input_chars", 40000),
                           timeout=config.get("request_timeout", 300),
                           max_output_tokens=config.get("max_output_tokens", 8192),
                           token_budget=config.get("token_budget"),
                           budget_path=root / ".iltkb" / "token_budget.json" if config.get("token_budget") is not None else None)
    return OllamaModel(model=config["model"], base_url=config.get("base_url", "http://localhost:11434"),
                       num_ctx=config.get("num_ctx", 32768),
                       max_input_chars=config.get("max_input_chars", 40000))


def research_summary(task: dict) -> dict:
    context = task["context"]
    return {"id": task["id"], "release": task["release"], "question": task["question"],
            "status": task["status"], "version": task["version"],
            "candidate": task["candidate"], "assessment": task["assessment"],
            "evidence_counts": {"proposer": context.get("proposer_evidence_count", len(context["evidence"])),
                                "additional": len(context["evidence"]) - context.get("proposer_evidence_count", len(context["evidence"])),
                                "omitted": context["omitted_evidence_count"]}}


def main(argv=None):
    parser = argparse.ArgumentParser(description="ILT Markdown knowledge compiler (LangChain/LangGraph + Gemini or Ollama)")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "build", "check", "candidate", "ontology-check", "ontology-graph", "ontology-export",
                 "search", "query", "context", "read", "serve",
                 "impact", "quarantine", "resolve", "incidents", "research-plan",
                 "research-critique", "research-assess", "research-list", "research-show"):
        command = commands.add_parser(name)
        command.add_argument("root", type=Path)
        if name == "init":
            command.add_argument("--provider", choices=("gemini", "ollama"), default="gemini")
        if name in ("search", "query", "context"):
            command.add_argument("question")
            command.add_argument("--limit", type=int, default=5)
        if name == "research-plan":
            command.add_argument("question", help="Research question or exact gap to investigate")
            command.add_argument("--topic", help="Optional topic ID to pin the research scope")
        if name in ("research-critique", "research-assess", "research-show"):
            command.add_argument("task_id")
        if name == "read":
            command.add_argument("topic", help="Topic ID (never an arbitrary filesystem path)")
        if name in ("impact", "quarantine", "resolve"):
            command.add_argument("source_id", help="Stable source ID from the release manifest")
        if name in ("quarantine", "resolve"):
            command.add_argument("--reason", required=True, help="Incident reason or reviewed resolution note")
        if name == "serve":
            command.add_argument("--host", default="127.0.0.1")
            command.add_argument("--port", type=int, default=8765)
            command.add_argument("--certfile", type=Path, help="TLS certificate for HTTPS")
            command.add_argument("--keyfile", type=Path, help="TLS private key for HTTPS")
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            initialize(args.root, args.provider)
            result = {"root": str(args.root.resolve()), "next": "Edit iltkb.json model; add Markdown/PDF under raw/"}
        elif args.command == "build":
            result = build(args.root, configured_model(args.root))
        elif args.command == "check":
            release = current_release(args.root)
            if release is None:
                raise ValueError("No published release")
            warnings = validate_release(release, args.root / ".iltkb" / "objects")
            result = {"valid": True, "release": release.name, "scope": "text-only", "warnings": warnings}
        elif args.command == "candidate":
            from .publication import candidate_report
            report = candidate_report(args.root)
            result = report if report else {"candidate": None}
        elif args.command == "ontology-check":
            from .ontology import OntologyStore
            report = OntologyStore(args.root).report()
            result = {key: report[key] for key in ("release", "schema_hash", "preview", "ready", "stale_count", "published")}
        elif args.command == "ontology-graph":
            from .ontology import published_graph
            result = published_graph(args.root)
        elif args.command == "ontology-export":
            from .ontology import export_ntriples
            sys.stdout.write(export_ntriples(args.root))
            return 0
        elif args.command == "serve":
            from .webapp import serve
            serve(args.root, args.host, args.port, args.certfile, args.keyfile)
            return 0
        elif args.command.startswith("research-"):
            from .research import ResearchStore, assess, continue_plan, plan
            if args.command == "research-plan":
                result = research_summary(plan(args.root, args.question, configured_model(args.root), topic_id=args.topic))
            elif args.command == "research-assess":
                result = research_summary(assess(args.root, args.task_id, configured_model(args.root)))
            elif args.command == "research-critique":
                result = research_summary(continue_plan(args.root, args.task_id, configured_model(args.root)))
            elif args.command == "research-show":
                result = ResearchStore(args.root).get(args.task_id)
            else:
                result = {"tasks": ResearchStore(args.root).list()}
        elif args.command in ("impact", "quarantine", "resolve", "incidents"):
            from .incidents import impact, incidents, quarantine, resolve
            if args.command == "impact":
                result = impact(args.root, args.source_id)
            elif args.command == "quarantine":
                result = quarantine(args.root, args.source_id, args.reason)
            elif args.command == "resolve":
                result = resolve(args.root, args.source_id, args.reason)
            else:
                result = incidents(args.root)
        else:
            reader = Reader(args.root)
            if args.command == "read":
                page = reader.manifest["pages"].get(args.topic)
                if page is None:
                    raise ValueError("Unknown topic ID")
                topic = page["topic"]
                print((reader.release / "wiki" / topic["group"] / (topic["id"] + ".md")).read_text(encoding="utf-8"))
                return 0
            if args.command == "search":
                result = {"release": reader.release.name, "matches": reader.search(args.question, args.limit)}
            elif args.command == "context":
                result = reader.context(args.question, args.limit)
            else:
                result = reader.answer(args.question, configured_model(args.root), args.limit)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(f"iltkb: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
