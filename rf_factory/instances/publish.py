"""Publish lightweight instances with one shared source per local batch."""
import json
import re
import shutil
import subprocess
from pathlib import Path

from ..core.common import FactoryError, digest, dumps, write_json
from ..repository import manifest


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
    base_id = None
    shared_base = None
    if not real:
        hashes = {row["repository"]["content_sha256"] for row in rows}
        if len(hashes) != 1 or not re.fullmatch(r"[0-9a-f]{64}", next(iter(hashes))):
            raise FactoryError("Local batch needs one valid shared source hash")
        base_id = next(iter(hashes))
        shared_base = output_root / "_bases" / base_id / "source"
        if not shared_base.exists():
            shared_base.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(staging / "source", shared_base)
        if digest(dumps(manifest(shared_base), sort_keys=True).encode()) != base_id:
            raise FactoryError("Shared source hash mismatch")
        base_image = "rf-factory-base:" + base_id[:16]
        base_dockerfile = shared_base.parent / "Dockerfile"
        base_dockerfile.write_text("FROM python:3.11-slim\nWORKDIR /work\nCOPY source/ /work/\n", encoding="utf-8")
        try:
            build = subprocess.run(["docker", "build", "-t", base_image, str(shared_base.parent)],
                                   capture_output=True, text=True, timeout=180)
            build_error = (build.stderr or build.stdout)[-1000:]
        except (OSError, subprocess.TimeoutExpired) as exc:
            build = None
            build_error = str(exc)
        image_id = None
        if build is not None and build.returncode == 0:
            try:
                inspect = subprocess.run(["docker", "image", "inspect", "--format", "{{.Id}}", base_image],
                                         capture_output=True, text=True, timeout=20)
                if inspect.returncode == 0:
                    image_id = inspect.stdout.strip()
            except (OSError, subprocess.TimeoutExpired) as exc:
                build_error = str(exc)
        write_json(shared_base.parent / "base.json", {"content_sha256": base_id, "image_name": base_image,
                   "image_id": image_id, "status": "ready" if image_id else "unavailable",
                   "error": None if image_id else build_error})
    published = []
    active_destination = None
    def skip_workspaces(directory, names):
        current = Path(directory)
        if current == staging:
            return ({"source", "acquired", "workspace", "post-history-source",
                     "partial-without-2-workspace"} & set(names)) | {
                name for name in names if name.startswith("synthetic-") and
                (name.endswith("-source") or name.endswith("-after"))}
        if current == staging / "historical-stage":
            return {"source", "acquired"} & set(names)
        if current.parent == staging / "attempts":
            return {name for name in names if name == "workspace" or name.startswith("step-")}
        if current == staging / "baseline":
            return {"workspace"} & set(names)
        if current.parent in {staging / "gold", staging / "precheck"}:
            return {"workspace", "noop_workspace"} & set(names)
        return set()

    try:
        for row, destination in zip(rows, destinations):
            active_destination = destination
            shutil.copytree(staging, destination, ignore=skip_workspaces)
            for path in (destination / "baseline" / "workspace",):
                if path.exists():
                    shutil.rmtree(path)
            for directory, name in (("gold", "workspace"), ("precheck", "noop_workspace")):
                parent = destination / directory
                if parent.exists():
                    for path in parent.glob(f"*/{name}"):
                        shutil.rmtree(path)
            if not real:
                row["repository"]["snapshot"] = None
                row["repository"]["shared_base"] = base_id
                row["environment"]["shared_base_image"] = base_image
                row["environment"]["shared_base_image_id"] = image_id
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
                dockerfile = (f"FROM {base_image}\n"
                              f"COPY verifier/{candidate_id}/ /verifier/\n"
                              'CMD ["python", "/verifier/runner.py", "/work", "/verifier/spec.json"]\n')
                (destination / row["environment"]["dockerfile"]).write_text(dockerfile, encoding="utf-8")
                (destination / "Dockerfile").write_text(dockerfile, encoding="utf-8")
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
                repo["snapshot"] = f"../_bases/{base_id}/source"
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
            else:
                environment = json.loads((destination / "environment.json").read_text(encoding="utf-8"))
                row["repository"] = {"repo_url": environment.get("repo_url"),
                                     "base_commit": environment["base_commit"],
                                     "image_id": environment["image_id"]}
                write_json(destination / "repository.json",
                           {**json.loads((destination / "repository.json").read_text(encoding="utf-8")),
                            "source_storage": "shared_docker_image", "image_id": environment["image_id"]})
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
