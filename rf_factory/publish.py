"""Publish successful synthesis runs as one self-contained directory per instance."""
import json
import re
import shutil
from pathlib import Path

from .common import FactoryError, write_json


def publish_run(staging: Path, output_root: Path) -> list[Path]:
    staging = staging.resolve(strict=True)
    output_root = output_root.resolve()
    rows = [json.loads(line) for line in (staging / "instances.jsonl").read_text(encoding="utf-8").splitlines() if line]
    if not rows:
        raise FactoryError("Run produced no instances to publish")
    ids = [row["instance_id"] for row in rows]
    if len(ids) != len(set(ids)) or any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value) for value in ids):
        raise FactoryError("Invalid or duplicate instance ID")
    if staging == output_root or staging.is_relative_to(output_root) or output_root.is_relative_to(staging):
        raise FactoryError("Staging and published output must be separate")
    output_root.mkdir(parents=True, exist_ok=True)
    destinations = [output_root / value for value in ids]
    if any(path.exists() for path in destinations):
        raise FileExistsError("Instance output already exists")

    real = (staging / "swe-bench-promax.json").exists()
    published = []
    active_destination = None
    try:
        for row, destination in zip(rows, destinations):
            active_destination = destination
            shutil.copytree(staging, destination)
            if not real:
                candidate_id = row["task_spec"]["candidate"]["candidate_id"]
                for directory in ("tasks", "gold", "precheck", "verifier", "docker"):
                    parent = destination / directory
                    if parent.exists():
                        for child in parent.iterdir():
                            if child.is_dir() and child.name != candidate_id:
                                shutil.rmtree(child)
                write_json(destination / "instances.json", [row])
                for filename, key in (("task_specs.json", "candidate"),):
                    path = destination / filename
                    if path.exists():
                        values = json.loads(path.read_text(encoding="utf-8"))
                        write_json(path, [item for item in values if item[key]["candidate_id"] == candidate_id])
                for filename in ("gold/patches.json", "precheck/results.json"):
                    path = destination / filename
                    if path.exists():
                        values = json.loads(path.read_text(encoding="utf-8"))
                        write_json(path, [item for item in values if item["candidate_id"] == candidate_id])
                docker_path = destination / "docker.json"
                if docker_path.exists():
                    docker = json.loads(docker_path.read_text(encoding="utf-8"))
                    docker["instances"] = [item for item in docker.get("instances", []) if item["instance_id"] == row["instance_id"]]
                    write_json(docker_path, docker)
                shutil.copy2(destination / row["environment"]["dockerfile"], destination / "Dockerfile")
                for filename in ("candidates.json", "selected_candidates.json"):
                    path = destination / filename
                    if path.exists():
                        values = json.loads(path.read_text(encoding="utf-8"))
                        write_json(path, [item for item in values if item["candidate_id"] == candidate_id])
                for filename in ("rejections.json", "candidate_rejections.json"):
                    path = destination / filename
                    if path.exists():
                        values = json.loads(path.read_text(encoding="utf-8"))
                        write_json(path, [item for item in values if item["candidate_id"] == candidate_id])
                repo_path = destination / "repository.json"
                repo = json.loads(repo_path.read_text(encoding="utf-8"))
                repo["snapshot"] = str(destination / "source")
                write_json(repo_path, repo)
                baseline_path = destination / "baseline" / "result.json"
                if baseline_path.exists():
                    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
                    baseline["argv"] = [str(destination / "baseline" / "tests.json") if value == str(staging / "baseline" / "tests.json")
                                        else value for value in baseline.get("argv", [])]
                    write_json(baseline_path, baseline)
                summary_path = destination / "summary.json"
                summary = json.loads(summary_path.read_text(encoding="utf-8"))
                summary.update(instance_id=row["instance_id"], candidates=1, gold_patch_generated=1,
                               gold_patch_success=1, gold_pass=1, noop_fail=1, exported=1,
                               rejected=0, output=str(destination / "instances.jsonl"))
                write_json(summary_path, summary)
            (destination / "instances.jsonl").write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
            published.append(destination)
            active_destination = None
    except Exception:
        if active_destination is not None and active_destination.exists():
            shutil.rmtree(active_destination)
        for path in published:
            shutil.rmtree(path)
        raise
    return published
