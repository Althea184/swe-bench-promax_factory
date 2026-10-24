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


def run_and_publish(args, config=None):
    output_root = args.output.resolve()
    work_root = output_root.parent / ".rf-factory-work"
    work_root.mkdir(parents=True, exist_ok=True)
    staging = work_root / uuid4().hex
    if args.command == "run-real":
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
