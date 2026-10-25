"""Export a shared base image once for transfer to an offline evaluator."""
import json
import re
import subprocess
from pathlib import Path

from ..core.common import FactoryError, write_json


def export_base(run: Path, output: Path):
    run = run.resolve(strict=True)
    output = output.resolve()
    if output.exists():
        raise FileExistsError(output)
    rows = [json.loads(line) for line in (run / "instances.jsonl").read_text(encoding="utf-8").splitlines() if line]
    if len(rows) != 1:
        raise FactoryError("Export a published single-instance directory")
    row = rows[0]
    image_id = row["repository"].get("image_id") or row["environment"].get("shared_base_image_id")
    if not image_id or not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
        raise FactoryError("Shared base image is not available locally")
    if row["repository"].get("image_id"):
        name = json.loads((run / "environment.json").read_text(encoding="utf-8"))["image_name"]
    else:
        name = row["environment"]["shared_base_image"]
    inspect = subprocess.run(["docker", "image", "inspect", "--format", "{{.Id}}", name],
                             capture_output=True, text=True, timeout=20)
    if inspect.returncode or inspect.stdout.strip() != image_id:
        raise FactoryError("Shared image tag does not match pinned image ID")
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        result = subprocess.run(["docker", "image", "save", "--output", str(output), name],
                                capture_output=True, text=True, timeout=1800)
        if result.returncode:
            raise FactoryError("Docker image export failed: " + result.stderr[-1000:])
        metadata = {"image_id": image_id, "image_name": name, "instance_id": row["instance_id"],
                    "archive": output.name, "bytes": output.stat().st_size}
        write_json(output.with_suffix(output.suffix + ".json"), metadata)
        return metadata
    except Exception:
        output.unlink(missing_ok=True)
        raise
