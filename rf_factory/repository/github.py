"""Fetch and resolve GitHub historical source records."""
import json
import os
import re
import time
import urllib.error
import urllib.request
from ..core.common import FactoryError

GITHUB_URL = re.compile(r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/(pull|issues)/([0-9]+)/?")
SHA = re.compile(r"[0-9a-fA-F]{40}")


def github_json(path):
    url = "https://api.github.com" + path
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "rf-factory-history"}
    if os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = "Bearer " + os.environ["GITHUB_TOKEN"]
    for attempt in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            raise FactoryError(f"GitHub API failed for {path}: {exc}") from exc
        except (OSError, ValueError) as exc:
            if attempt == 2 or isinstance(exc, ValueError):
                raise FactoryError(f"GitHub API failed for {path}: {exc}") from exc
            time.sleep(0.5 * (attempt + 1))


def github_pages(path, getter=github_json):
    values = []
    for page in range(1, 21):
        suffix = "&" if "?" in path else "?"
        chunk = getter(f"{path}{suffix}per_page=100&page={page}")
        if not isinstance(chunk, list):
            raise FactoryError("GitHub paginated response was not a list")
        values.extend(chunk)
        if len(chunk) < 100:
            return values
    raise FactoryError("GitHub pagination exceeded 2000 records")


def resolve_pr(*, pr_url=None, repo_url=None, commit=None, issue_url=None, getter=github_json):
    selected = sum(value is not None for value in (pr_url, commit, issue_url))
    if selected != 1:
        raise FactoryError("Specify exactly one PR URL, commit SHA, or issue URL")
    if pr_url or issue_url:
        match = GITHUB_URL.fullmatch(pr_url or issue_url)
        expected = "pull" if pr_url else "issues"
        if not match or match.group(3) != expected:
            raise FactoryError("Invalid GitHub PR or Issue URL")
        owner, name, _, number = match.groups()
        repo_url = f"https://github.com/{owner}/{name}"
        if issue_url:
            events = github_pages(f"/repos/{owner}/{name}/issues/{number}/timeline", getter)
            issue_api = f"https://api.github.com/repos/{owner}/{name}/issues/"
            numbers = sorted({issue.get("number") for event in events
                              for issue in [event.get("source", {}).get("issue", {})]
                              if issue.get("pull_request") and
                              (issue.get("url") or "").startswith(issue_api)})
            numbers = [value for value in numbers if isinstance(value, int)]
            if len(numbers) != 1:
                raise FactoryError("Issue has no unique cross-referenced PR; no historical Gold inferred")
            number = numbers[0]
    else:
        if not repo_url or not re.fullmatch(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo_url):
            raise FactoryError("Commit mode requires a public GitHub --repo-url")
        if not SHA.fullmatch(commit):
            raise FactoryError("Commit must be a full 40-character SHA")
        owner, name = repo_url.split("/")[-2:]
        pulls = getter(f"/repos/{owner}/{name}/commits/{commit}/pulls")
        matches = [item for item in pulls if item.get("merged_at") and
                   item.get("merge_commit_sha") == commit]
        if len(matches) != 1:
            raise FactoryError("Commit does not resolve to one merged PR")
        number = matches[0]["number"]
    pr = getter(f"/repos/{owner}/{name}/pulls/{number}")
    if not pr.get("merged") or not SHA.fullmatch(pr.get("merge_commit_sha") or ""):
        raise FactoryError("PR is not merged with a full merge commit SHA")
    commits = github_pages(f"/repos/{owner}/{name}/pulls/{number}/commits", getter)
    files = github_pages(f"/repos/{owner}/{name}/pulls/{number}/files", getter)
    return {"repo_url": repo_url, "owner": owner, "name": name, "number": int(number),
            "url": f"https://github.com/{owner}/{name}/pull/{number}",
            "title": pr["title"], "description": pr.get("body") or "",
            "merged_at": pr["merged_at"], "merge_commit": pr["merge_commit_sha"],
            "head_sha": pr["head"]["sha"], "pr_commits": [item["sha"] for item in commits],
            "pr_files": [item["filename"] for item in files],
            "linked_issue": issue_url}


