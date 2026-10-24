"""Export only solver-visible material from a controller-side batch run."""
import json
import shutil
import subprocess
import re
from pathlib import Path

from .common import FactoryError, digest, dumps, write_json
from .repository import manifest


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
    repository = row["repository"]
    source = None
    image_id = repository.get("image_id")
    if image_id:
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
            raise FactoryError("Invalid shared image ID")
    elif repository.get("shared_base"):
        base = repository["shared_base"]
        if not re.fullmatch(r"[0-9a-f]{64}", base):
            raise FactoryError("Invalid shared base ID")
        source = (run.parent / "_bases" / base / "source").resolve(strict=True)
        if not source.is_relative_to(run.parent / "_bases"):
            raise FactoryError("Invalid shared base path")
        if digest(dumps(manifest(source), sort_keys=True).encode()) != base:
            raise FactoryError("Shared base content changed")
    else:
        source = (run / repository["snapshot"]).resolve(strict=True)
        if not source.is_relative_to(run) or source != run / "source":
            raise FactoryError("Invalid repository snapshot path")
    if output.exists():
        raise FileExistsError(output)
    output.mkdir(parents=True)
    try:
        if image_id:
            created = subprocess.run(["docker", "create", image_id], capture_output=True, text=True, timeout=30)
            if created.returncode:
                raise FactoryError("Shared base image unavailable: " + created.stderr.strip())
            container = created.stdout.strip()
            try:
                (output / "repository").mkdir()
                copied = subprocess.run(["docker", "cp", f"{container}:/testbed/.", str(output / "repository")],
                                        capture_output=True, text=True, timeout=180)
                if copied.returncode:
                    raise FactoryError("Cannot export repository from base image: " + copied.stderr.strip())
                commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=output / "repository",
                                        capture_output=True, text=True, timeout=20)
                if commit.returncode or commit.stdout.strip() != repository["base_commit"]:
                    raise FactoryError("Base image repository commit mismatch")
            finally:
                subprocess.run(["docker", "rm", "-f", container], capture_output=True, timeout=30)
        else:
            shutil.copytree(source, output / "repository")
        (output / "task.md").write_text(row["problem_statement"]["text"] + "\n", encoding="utf-8")
        write_json(output / "environment.json", {"python": row["environment"]["python"],
                                                  "dependencies": row["environment"]["dependencies"]})
    except Exception:
        shutil.rmtree(output)
        raise
    return {"instance_id": instance_id, "output": str(output), "repository": str(output / "repository"),
            "task": str(output / "task.md")}
