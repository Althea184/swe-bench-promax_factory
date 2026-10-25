"""Build and run the existing local fixture Docker artifacts."""
import json
import shutil
import subprocess
from pathlib import Path


def build_local_docker(output, instances):
    dockerfile = output / "Dockerfile"
    (output / ".dockerignore").write_text("*\n!Dockerfile\n!docker/\n!docker/**\n!source/\n!source/**\n!verifier/\n!verifier/**\n", encoding="utf-8")
    if not instances:
        dockerfile.write_text("FROM python:3.11-slim\nWORKDIR /work\nCOPY source/ /work/\n", encoding="utf-8")
        return {"status": "not_run", "reason": "no valid instance"}
    entries = []
    for instance in instances:
        cid = instance.task_spec.candidate.candidate_id
        path = output / instance.environment["dockerfile"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("FROM python:3.11-slim\nWORKDIR /work\nCOPY source/ /work/\n"
                        f"COPY verifier/{cid}/ /verifier/\n"
                        'CMD ["python", "/verifier/runner.py", "/work", "/verifier/spec.json"]\n', encoding="utf-8")
        entries.append({"instance_id": instance.instance_id, "dockerfile": instance.environment["dockerfile"],
                        "image": "rf-factory:" + instance.instance_id})
    shutil.copy2(output / entries[0]["dockerfile"], dockerfile)
    try:
        version = subprocess.run(["docker", "version", "--format", "{{.Server.Version}}"], capture_output=True, text=True, timeout=15)
        if version.returncode:
            return {"status": "not_available", "reason": version.stderr.strip() or version.stdout.strip(), "instances": entries}
        for entry in entries:
            build = subprocess.run(["docker", "build", "-t", entry["image"], "-f", str(output / entry["dockerfile"]), str(output)],
                                   capture_output=True, text=True, timeout=180)
            slot = output / Path(entry["dockerfile"]).parent
            (slot / "build.stdout.txt").write_text(build.stdout, encoding="utf-8")
            (slot / "build.stderr.txt").write_text(build.stderr, encoding="utf-8")
            if build.returncode:
                entry.update(status="build_failed", returncode=build.returncode)
                continue
            run = subprocess.run(["docker", "run", "--rm", entry["image"]], capture_output=True, text=True, timeout=60)
            (slot / "run.stdout.txt").write_text(run.stdout, encoding="utf-8")
            (slot / "run.stderr.txt").write_text(run.stderr, encoding="utf-8")
            result = json.loads(run.stdout) if run.stdout.strip() else {}
            entry["status"] = "built_and_ran" if run.returncode == 1 and result.get("checks") and not all(result["checks"].values()) else "run_failed"
        return {"status": "built_and_ran" if all(item["status"] == "built_and_ran" for item in entries) else "partial_failure",
                "instances": entries}
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        return {"status": "not_available", "reason": str(exc), "instances": entries}
