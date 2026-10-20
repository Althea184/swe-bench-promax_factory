"""Export only solver-visible material from a controller-side batch run."""
import json
import shutil
from pathlib import Path

from .common import FactoryError, write_json


def prepare_rollout(run: Path, instance_id: str, output: Path):
    run = run.resolve(strict=True)
    output = output.resolve()
    if output.is_relative_to(run) or run.is_relative_to(output):
        raise FactoryError("Rollout output must be separate from the controller run")
    rows = [json.loads(line) for line in (run / "instances.jsonl").read_text(encoding="utf-8").splitlines() if line]
    matches = [row for row in rows if row["instance_id"] == instance_id]
    if len(matches) != 1:
        raise FactoryError("Instance ID missing or ambiguous")
    row = matches[0]
    source = (run / row["repository"]["snapshot"]).resolve(strict=True)
    if not source.is_relative_to(run) or source != run / "source":
        raise FactoryError("Invalid repository snapshot path")
    if output.exists():
        raise FileExistsError(output)
    output.mkdir(parents=True)
    try:
        shutil.copytree(source, output / "repository")
        (output / "task.md").write_text(row["problem_statement"]["text"] + "\n", encoding="utf-8")
        write_json(output / "environment.json", {"python": row["environment"]["python"],
                                                  "dependencies": row["environment"]["dependencies"]})
    except Exception:
        shutil.rmtree(output)
        raise
    return {"instance_id": instance_id, "output": str(output), "repository": str(output / "repository"),
            "task": str(output / "task.md")}
