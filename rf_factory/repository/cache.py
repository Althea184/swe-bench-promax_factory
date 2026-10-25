"""Reusable GitHub response and historical Git object caches."""
import json
from pathlib import Path
from ..core.common import digest, write_json
from ..core.process import command, require
from .github import github_json


class CachedGitHub:
    def __init__(self, cache: Path, fetcher=None):
        self.fetcher = fetcher or github_json
        self.cache = cache / "api"
        self.cache.mkdir(parents=True, exist_ok=True)

    def __call__(self, path):
        destination = self.cache / (digest(path.encode()) + ".json")
        if destination.exists():
            return json.loads(destination.read_text(encoding="utf-8"))
        result = self.fetcher(path)
        write_json(destination, result)
        return result


def cached_clone(cache: Path, record, local_paths):
    repo_url = record["repo_url"]
    destination = cache / "repos" / (record["owner"] + "__" + record["name"])
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        source = local_paths.get(repo_url, repo_url)
        argv = ["git", "-c", "core.autocrlf=false", "clone", "--no-hardlinks", "--no-checkout"]
        if source == repo_url:
            argv.append("--filter=blob:none")
        require(command([*argv, "--", str(source), str(destination)], timeout=600),
                "Cached history clone")
        require(command(["git", "config", "core.autocrlf", "false"], cwd=destination),
                "Cached history newline configuration")
    if command(["git", "cat-file", "-e", record["merge_commit"] + "^{commit}"],
               cwd=destination).returncode:
        require(command(["git", "fetch", "origin", record["merge_commit"]],
                        cwd=destination, timeout=600), "Historical commit fetch")
    return destination


