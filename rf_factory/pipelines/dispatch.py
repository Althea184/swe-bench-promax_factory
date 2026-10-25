"""Dispatch synthesis routes and publish completed runs."""
import shutil
from uuid import uuid4
from .local import Pipeline
from .real import RealPipeline
from .historical import HistoryPipeline
from .historical_compound import HistoryCompoundPipeline
from .hybrid import HybridCompoundPipeline
from ..repository.cache import CachedGitHub
from ..instances.publish import publish_run


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


