import argparse
import logging
from pathlib import Path
import shutil
import sys
from uuid import uuid4
from .common import FactoryError, dumps
from .config import Config
from .pipeline import Pipeline
from .rollout import prepare_rollout
from .real_pipeline import RealPipeline
from .publish import publish_run
from .base_image import export_base
from .agent_runner import run_agent
from .history import HistoryPipeline
from .history_scan import scan_history
from .history_scan import CachedGitHub
from .history_compound import HistoryCompoundPipeline
from .hybrid_compound import HybridCompoundPipeline


def run_and_publish(args, config=None):
    output_root = args.output.resolve()
    work_root = output_root.parent / ".rf-factory-work"
    work_root.mkdir(parents=True, exist_ok=True)
    staging = work_root / uuid4().hex
    if args.command == "run-history":
        import shlex
        getter = CachedGitHub(args.cache.resolve()) if args.cache else None
        if args.preflight:
            kwargs = {"getter": getter} if getter else {}
            return HistoryPipeline().run(output=output_root, pr_url=args.pr_url,
                repo_url=args.repo_url, commit=args.commit, issue_url=args.issue_url,
                local_path=args.local_path, preflight=True, base_image=args.base_image,
                offline_build=args.offline_build, test_argv=shlex.split(args.test_command), **kwargs)
        kwargs = {"getter": getter} if getter else {}
        result = HistoryPipeline().run(output=staging, pr_url=args.pr_url,
            repo_url=args.repo_url, commit=args.commit, issue_url=args.issue_url,
            local_path=args.local_path, base_image=args.base_image,
            offline_build=args.offline_build, test_argv=shlex.split(args.test_command), **kwargs)
    elif args.command == "run-history-compound":
        import shlex
        destination = output_root if args.preflight else staging
        result = HistoryCompoundPipeline().run(output=destination,
            pr_urls=args.pr_urls, commits=args.commits, repo_url=args.repo_url,
            local_path=args.local_path, cache=args.cache,
            preflight=args.preflight, base_image=args.base_image,
            offline_build=args.offline_build, test_argv=shlex.split(args.test_command))
        if args.preflight:
            return result
    elif args.command == "run-hybrid-compound":
        import shlex
        result = HybridCompoundPipeline().run(output=staging, commits=args.commits,
            repo_url=args.repo_url, local_path=args.local_path,
            base_image=args.base_image, offline_build=args.offline_build,
            test_argv=shlex.split(args.test_command))
    elif args.command == "run-real":
        import shlex
        if args.preflight:
            return RealPipeline().run(repo_url=args.repo_url, output=output_root,
                                      commit=args.commit, local_path=args.local_path,
                                      test_argv=shlex.split(args.test_command),
                                      max_candidates=args.max_candidates,
                                      base_image=args.base_image,
                                      offline_build=args.offline_build,
                                      compound=args.compound, preflight=True)
        result = RealPipeline().run(repo_url=args.repo_url, output=staging, commit=args.commit,
                                    local_path=args.local_path, test_argv=shlex.split(args.test_command),
                                    max_candidates=args.max_candidates, base_image=args.base_image,
                                    offline_build=args.offline_build, compound=args.compound)
    else:
        result = Pipeline().run(config, staging)
    if result["status"] != "completed":
        result["work_directory"] = str(staging)
        return result
    destinations = publish_run(staging, output_root)
    if staging.resolve().is_relative_to(work_root.resolve()):
        shutil.rmtree(staging)
    result["instance_directories"] = [str(path) for path in destinations]
    if len(destinations) == 1:
        result["output"] = str(destinations[0] / "instances.jsonl")
    else:
        result.pop("output", None)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="Experimental RF instance synthesis")
    commands = parser.add_subparsers(dest="command", required=True)
    demo = commands.add_parser("demo", help="Run the full pipeline on the bundled API migration fixture")
    demo.add_argument("--output", required=True, type=Path, help="Directory containing instance-ID folders")
    run = commands.add_parser("run", help="Run against a trusted local repository")
    run.add_argument("--config", required=True, type=Path)
    run.add_argument("--output", required=True, type=Path, help="Directory containing instance-ID folders")
    real = commands.add_parser("run-real", help="Synthesize from a fixed public GitHub Python repository in Docker")
    real.add_argument("--repo-url", required=True)
    real.add_argument("--commit")
    real.add_argument("--local-path", type=Path, help="Existing local clone of the GitHub repository")
    real.add_argument("--output", required=True, type=Path, help="Directory containing instance-ID folders")
    real.add_argument("--test-command", default="python -m unittest discover -s tests")
    real.add_argument("--max-candidates", type=int, default=5)
    real.add_argument("--base-image", default="python:3.11-slim")
    real.add_argument("--offline-build", action="store_true")
    real.add_argument("--compound", action="store_true", help="Compose two related API migrations")
    real.add_argument("--preflight", action="store_true", help="Report compound discovery before Docker build")
    history = commands.add_parser("run-history", help="Mine a merged GitHub refactoring PR")
    source = history.add_mutually_exclusive_group(required=True)
    source.add_argument("--pr-url")
    source.add_argument("--commit")
    source.add_argument("--issue-url")
    history.add_argument("--repo-url", help="Required with --commit")
    history.add_argument("--local-path", type=Path, help="Clean clone of the same GitHub repository")
    history.add_argument("--output", required=True, type=Path)
    history.add_argument("--preflight", action="store_true")
    history.add_argument("--test-command", default="python -m pytest -q tests")
    history.add_argument("--base-image", default="python:3.11-slim")
    history.add_argument("--offline-build", action="store_true")
    history.add_argument("--cache", type=Path, help="Reuse metadata cached by history-scan")
    history_compound = commands.add_parser("run-history-compound",
        help="Compose two related consecutive historical refactorings")
    historical_sources = history_compound.add_mutually_exclusive_group(required=True)
    historical_sources.add_argument("--pr-urls", nargs=2)
    historical_sources.add_argument("--commits", nargs=2)
    history_compound.add_argument("--repo-url", help="Required with --commits")
    history_compound.add_argument("--local-path", type=Path)
    history_compound.add_argument("--cache", type=Path)
    history_compound.add_argument("--output", required=True, type=Path)
    history_compound.add_argument("--preflight", action="store_true")
    history_compound.add_argument("--test-command", default="python -m pytest -q tests")
    history_compound.add_argument("--base-image", default="python:3.11-slim")
    history_compound.add_argument("--offline-build", action="store_true")
    hybrid = commands.add_parser("run-hybrid-compound",
        help="Compose two historical relocations and dependent API migrations")
    hybrid.add_argument("--commits", nargs=2, required=True)
    hybrid.add_argument("--repo-url", required=True)
    hybrid.add_argument("--local-path", type=Path)
    hybrid.add_argument("--output", required=True, type=Path)
    hybrid.add_argument("--test-command", default="python -m pytest -q tests")
    hybrid.add_argument("--base-image", default="python:3.11-slim")
    hybrid.add_argument("--offline-build", action="store_true")
    scan = commands.add_parser("history-scan", help="Preflight multiple merged GitHub refactoring PRs")
    scan.add_argument("--input", required=True, type=Path, help="JSON or newline-separated PR URLs")
    scan.add_argument("--output", required=True, type=Path)
    scan.add_argument("--cache", type=Path, default=Path(".rf-factory-work/history-cache"))
    scan.add_argument("--local-path-map", type=Path,
                      help="Optional JSON mapping of GitHub repository URLs to clean local clones")
    check = commands.add_parser("validate-config")
    check.add_argument("--config", required=True, type=Path)
    rollout = commands.add_parser("prepare-rollout", help="Export base repository and solver-visible task only")
    rollout.add_argument("--run", required=True, type=Path)
    rollout.add_argument("--instance", required=True)
    rollout.add_argument("--output", required=True, type=Path)
    bundle = commands.add_parser("export-base", help="Save the shared Docker base image for offline transfer")
    bundle.add_argument("--run", required=True, type=Path)
    bundle.add_argument("--output", required=True, type=Path)
    agent = commands.add_parser("run-agent", help="Run an external solver and Docker-evaluate its patch")
    agent.add_argument("--run", required=True, type=Path, help="Published real instance directory")
    agent.add_argument("--instance", required=True)
    agent.add_argument("--output", required=True, type=Path)
    agent.add_argument("--agent-command", required=True,
                       help="Trusted command; {task}, {repo}, {max_turns}, {model} placeholders are available")
    agent.add_argument("--model", default="external-agent")
    agent.add_argument("--repetitions", type=int, default=1)
    agent.add_argument("--max-turns", type=int, default=20)
    agent.add_argument("--agent-timeout", type=int, default=1800)
    agent.add_argument("--eval-timeout", type=int, default=1800)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        if args.command == "prepare-rollout":
            result = prepare_rollout(args.run, args.instance, args.output)
            print(dumps(result, indent=2))
            return 0
        if args.command == "export-base":
            print(dumps(export_base(args.run, args.output), indent=2))
            return 0
        if args.command == "run-agent":
            result = run_agent(run=args.run, instance_id=args.instance, output=args.output,
                               agent_command=args.agent_command, model=args.model,
                               repetitions=args.repetitions, max_turns=args.max_turns,
                               agent_timeout=args.agent_timeout, eval_timeout=args.eval_timeout)
            print(dumps(result, indent=2))
            return 0
        if args.command == "run-real":
            result = run_and_publish(args)
            print(dumps(result, indent=2))
            return 1 if result.get("status") == "preflight_completed" and not result.get("pair_count") else 0
        if args.command == "run-history":
            result = run_and_publish(args)
            print(dumps(result, indent=2))
            return 0 if result.get("status") in {"completed", "preflight_completed"} else 1
        if args.command == "run-history-compound":
            result = run_and_publish(args)
            print(dumps(result, indent=2))
            return 0 if result.get("status") in {"completed", "preflight_completed"} else 1
        if args.command == "run-hybrid-compound":
            result = run_and_publish(args)
            print(dumps(result, indent=2))
            return 0 if result.get("status") == "completed" else 1
        if args.command == "history-scan":
            import json
            local_paths = json.loads(args.local_path_map.read_text(encoding="utf-8-sig")) if args.local_path_map else {}
            if not isinstance(local_paths, dict):
                raise FactoryError("Local path map must be a JSON object")
            result = scan_history(input_path=args.input, output=args.output,
                                  cache=args.cache, local_paths=local_paths)
            print(dumps(result, indent=2))
            return 0
        if args.command == "demo":
            config = Config(Path(__file__).with_name("fixtures") / "api_migration_repo")
        else:
            config = Config.load(args.config)
        result = {"valid": True} if args.command == "validate-config" else run_and_publish(args, config)
        print(dumps(result, indent=2))
        return 1 if result.get("status") == "no_valid_instances" else 0
    except (FactoryError, OSError, ValueError, TypeError) as exc:
        print(dumps({"error": str(exc), "type": type(exc).__name__}), file=sys.stderr)
        return 2
