import argparse
import logging
from pathlib import Path
import sys
from .common import FactoryError, dumps
from .config import Config
from .pipeline import Pipeline


def main(argv=None):
    parser = argparse.ArgumentParser(description="Experimental RF instance synthesis")
    commands = parser.add_subparsers(dest="command", required=True)
    demo = commands.add_parser("demo", help="Run the full pipeline on the bundled API migration fixture")
    demo.add_argument("--output", required=True, type=Path)
    run = commands.add_parser("run", help="Run against a trusted local repository")
    run.add_argument("--config", required=True, type=Path)
    run.add_argument("--output", required=True, type=Path)
    check = commands.add_parser("validate-config")
    check.add_argument("--config", required=True, type=Path)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        if args.command == "demo":
            config = Config(Path(__file__).with_name("fixtures") / "api_migration_repo")
        else:
            config = Config.load(args.config)
        result = {"valid": True} if args.command == "validate-config" else Pipeline().run(config, args.output)
        print(dumps(result, indent=2))
        return 1 if result.get("status") == "no_valid_instances" else 0
    except (FactoryError, OSError, ValueError, TypeError) as exc:
        print(dumps({"error": str(exc), "type": type(exc).__name__}), file=sys.stderr)
        return 2
