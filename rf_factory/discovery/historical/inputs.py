"""Validate batches of historical PR source URLs."""
import json
from pathlib import Path
from ...core.common import FactoryError
from ...repository.github import GITHUB_URL


def read_pr_urls(path: Path):
    text = path.read_text(encoding="utf-8-sig")
    if path.suffix.lower() == ".json":
        data = json.loads(text)
        values = data.get("prs") if isinstance(data, dict) else data
    else:
        values = [line.strip() for line in text.splitlines()
                  if line.strip() and not line.lstrip().startswith("#")]
    if not isinstance(values, list) or not values or any(not isinstance(value, str) for value in values):
        raise FactoryError("PR input must contain a nonempty list of URLs")
    if len(values) > 100 or len(values) != len(set(values)):
        raise FactoryError("PR input must contain at most 100 distinct URLs")
    for value in values:
        match = GITHUB_URL.fullmatch(value)
        if not match or match.group(3) != "pull":
            raise FactoryError(f"Invalid GitHub PR URL: {value}")
    return values


