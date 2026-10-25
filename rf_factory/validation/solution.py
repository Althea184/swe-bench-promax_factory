"""Evaluate a model patch in a fresh offline Docker container."""
import re


def evaluate_solution(image_id, patch_path, script_path, attempt, timeout, patterns=(), *, command, require):
    """Apply the prediction in a fresh offline container, then run eval.json."""
    checks = "".join(f"; python -m unittest discover -s rf_acceptance -p {pattern}"
                     f"; echo RF_SUBTASK_{index}_EXIT_CODE=$?"
                     for index, pattern in enumerate(patterns))
    shell = ("if ! git apply --check /tmp/rf-model.patch; then "
             "echo RF_MODEL_PATCH_APPLY_FAILED=1; exit 2; fi; "
             "git apply /tmp/rf-model.patch || exit 2; "
             "bash /tmp/rf-eval.sh; rf_full_rc=$?" + checks + "; exit $rf_full_rc")
    created = command(["docker", "create", "--network", "none", "--workdir", "/testbed",
                        image_id, "bash", "-c", shell], timeout=60)
    container = require(created, "Docker container creation")
    try:
        for source, target in ((patch_path, "/tmp/rf-model.patch"),
                               (script_path, "/tmp/rf-eval.sh")):
            require(command(["docker", "cp", str(source), f"{container}:{target}"], timeout=60),
                     "Docker input copy")
        result = command(["docker", "start", "-a", container], timeout=timeout)
        (attempt / "eval.stdout.txt").write_text(result.stdout, encoding="utf-8")
        (attempt / "eval.stderr.txt").write_text(result.stderr, encoding="utf-8")
        if result.returncode == 124:
            return {"status": "timed_out", "returncode": 124}
        exit_code = int(require(command(["docker", "inspect", "--format",
                                           "{{.State.ExitCode}}", container], timeout=30),
                                 "Docker exit inspection"))
        status = "resolved" if exit_code == 0 else "unresolved"
        if "RF_MODEL_PATCH_APPLY_FAILED=1" in result.stdout + result.stderr:
            status = "invalid_patch"
        elif "OMNIGRIL_TEST_PATCH_APPLY_FAILED=1" in result.stdout + result.stderr:
            status = "evaluation_error"
        subtask_codes = {int(index): int(code) for index, code in
                         re.findall(r"RF_SUBTASK_(\d+)_EXIT_CODE=(\d+)", result.stdout)}
        if patterns and len(subtask_codes) != len(patterns) and status not in {"evaluation_error", "invalid_patch"}:
            status = "evaluation_error"
        return {"status": status, "returncode": exit_code,
                "subtasks": [{"test": pattern, "returncode": subtask_codes.get(index),
                              "passed": subtask_codes.get(index) == 0}
                             for index, pattern in enumerate(patterns)]}
    finally:
        command(["docker", "rm", "-f", container], timeout=30)


