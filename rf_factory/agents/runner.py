"""Run a trusted external solver against a published real instance."""
from __future__ import annotations

import json
import math
import os
import re
import shlex
import shutil
import stat
import subprocess
import time
from pathlib import Path

from ..core.common import FactoryError, write_json
from .rollout import prepare_rollout
from ..validation.solution import evaluate_solution


def _command(argv, *, cwd=None, timeout=60, input_text=None, env=None):
    try:
        return subprocess.run(argv, cwd=cwd, input=input_text, capture_output=True,
                              text=True, encoding="utf-8", errors="replace",
                              timeout=timeout, env=env, check=False)
    except subprocess.TimeoutExpired as exc:
        def decoded(value):
            return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value or ""
        return subprocess.CompletedProcess(argv, 124, decoded(exc.stdout),
                                           decoded(exc.stderr) + f"\nTIMEOUT AFTER {timeout}s")
    except OSError as exc:
        return subprocess.CompletedProcess(argv, 127, "", str(exc))


def _require(result, operation):
    if result.returncode:
        raise FactoryError(f"{operation} failed ({result.returncode}): {result.stderr[-1000:]}")
    return result.stdout.strip()


def _git(repository, *args, timeout=30):
    return _command(["git", "-C", str(repository), *args], timeout=timeout)


def _load_instance(run, instance_id):
    rows = [json.loads(line) for line in (run / "instances.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()]
    matches = [row for row in rows if row.get("instance_id") == instance_id]
    if len(matches) != 1:
        raise FactoryError("Instance ID missing or ambiguous")
    row = matches[0]
    image_id = row.get("repository", {}).get("image_id")
    if not image_id:
        raise FactoryError("Agent Runner currently requires a published Docker-backed real instance")
    evaluation = json.loads((run / "eval.json").read_text(encoding="utf-8"))
    script = evaluation.get(instance_id, {}).get("eval_script")
    if not isinstance(script, str) or not script.strip():
        raise FactoryError("Instance evaluation script is missing")
    return row, image_id, script


def _agent_argv(command, task, repository, max_turns, model):
    argv = shlex.split(command)
    if not argv:
        raise FactoryError("Agent command is empty")
    values = {"{task}": task, "{repo}": str(repository),
              "{max_turns}": str(max_turns), "{model}": model}
    return [next((value for key, value in values.items() if token == key), token)
            for token in argv]


def _agent_report(path):
    if not path.exists():
        return {}
    report = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(report, dict):
        raise FactoryError("Agent result must be a JSON object")
    for key in ("turns", "input_tokens", "output_tokens"):
        if key in report and (type(report[key]) is not int or report[key] < 0):
            raise FactoryError(f"Agent result {key} must be a nonnegative integer")
    if "cost_usd" in report and (isinstance(report["cost_usd"], bool)
                                  or not isinstance(report["cost_usd"], (int, float))
                                  or not math.isfinite(report["cost_usd"])
                                  or report["cost_usd"] < 0):
        raise FactoryError("Agent result cost_usd must be nonnegative")
    return {key: report[key] for key in ("turns", "input_tokens", "output_tokens", "cost_usd")
            if key in report}


def _codex_cli_usage(stdout):
    """Read token counts from Codex CLI JSONL when an adapter report is absent."""
    totals = {"input_tokens": 0, "output_tokens": 0}
    completed = 0
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("type") != "turn.completed":
            continue
        usage = event.get("usage")
        if not isinstance(usage, dict):
            continue
        if any(type(usage.get(key)) is not int or usage[key] < 0 for key in totals):
            continue
        completed += 1
        for key in totals:
            totals[key] += usage[key]
    return totals if completed else {}


def _collect_patch(repository, base_commit, forbidden_paths):
    head = _require(_git(repository, "rev-parse", "HEAD"), "Solver HEAD inspection")
    _require(_git(repository, "merge-base", "--is-ancestor", base_commit, head),
             "Solver base ancestry check")
    _require(_git(repository, "add", "-N", "--all"), "Untracked file discovery")
    changed = _require(_git(repository, "diff", "--name-only", "-z", base_commit),
                       "Solver changed-file inspection")
    paths = [path for path in changed.split("\0") if path]
    protected = [path for path in paths if path in forbidden_paths or
                 path.startswith(("rf_acceptance/", "tests/", "test/"))]
    if protected:
        raise FactoryError("Solver modified protected test files: " + ", ".join(protected))
    patch = _require(_git(repository, "diff", "--binary", "--no-ext-diff", base_commit,
                          timeout=120), "Solver patch collection")
    return patch + "\n" if patch else "", paths


def _acceptance_patterns(test_patch):
    paths = [line.removeprefix("+++ b/") for line in test_patch.splitlines()
             if line.startswith("+++ b/")]
    return [path.rsplit("/", 1)[-1] for path in paths
            if re.fullmatch(r"rf_acceptance/test_rf_(?:migration|history)_[0-9]+\.py", path)]


def _evaluate(image_id, patch_path, script_path, attempt, timeout, patterns=()):
    return evaluate_solution(image_id, patch_path, script_path, attempt, timeout,
                             patterns, command=_command, require=_require)


def _remove_rollout(path):
    def writable_remove(operation, target, _error):
        os.chmod(target, stat.S_IWRITE)
        operation(target)
    shutil.rmtree(path, onerror=writable_remove)


def run_agent(*, run: Path, instance_id: str, output: Path, agent_command: str,
              model: str = "external-agent", repetitions: int = 1, max_turns: int = 20,
              agent_timeout: int = 1800, eval_timeout: int = 1800):
    if min(repetitions, max_turns, agent_timeout, eval_timeout) < 1:
        raise FactoryError("Repetitions, max turns and timeouts must be positive")
    run = run.resolve(strict=True)
    output = output.resolve()
    if output.exists() or output.is_relative_to(run) or run.is_relative_to(output):
        raise FactoryError("Runner output must be a new directory separate from the instance")
    row, image_id, eval_script = _load_instance(run, instance_id)
    if not shlex.split(agent_command):
        raise FactoryError("Agent command is empty")
    output.mkdir(parents=True)
    results = []
    for number in range(repetitions):
        attempt = output / "attempts" / f"{number:03d}"
        attempt.mkdir(parents=True)
        rollout = attempt / "rollout"
        record = {"attempt": number, "status": "error", "model": model,
                  "max_turns": max_turns, "agent_timeout": agent_timeout}
        try:
            prepare_rollout(run, instance_id, rollout)
            repository = rollout / "repository"
            task = (rollout / "task.md").read_text(encoding="utf-8")
            report_path = rollout / "agent-result.json"
            env = os.environ.copy()
            env.update(RF_TASK_FILE=str(rollout / "task.md"), RF_REPOSITORY=str(repository),
                       RF_RESULT_FILE=str(report_path), RF_MAX_TURNS=str(max_turns),
                       RF_MODEL=model)
            argv = _agent_argv(agent_command, task, repository, max_turns, model)
            started = time.monotonic()
            agent = _command(argv, cwd=repository, timeout=agent_timeout,
                             input_text=task, env=env)
            record["agent_seconds"] = round(time.monotonic() - started, 3)
            record["agent_returncode"] = agent.returncode
            (attempt / "agent.stdout.txt").write_text(agent.stdout, encoding="utf-8")
            (attempt / "agent.stderr.txt").write_text(agent.stderr, encoding="utf-8")
            record.update(_agent_report(report_path))
            for key, value in _codex_cli_usage(agent.stdout).items():
                record.setdefault(key, value)
            base = row["repository"]["base_commit"]
            test_patch = (run / "test.patch").read_text(encoding="utf-8")
            protected = {line.removeprefix("+++ b/") for line in test_patch.splitlines()
                         if line.startswith("+++ b/")}
            patch, changed = _collect_patch(repository, base, protected)
            record["changed_files"] = changed
            (attempt / "model.patch").write_bytes(patch.encode("utf-8"))
            write_json(attempt / "prediction.json", {"instance_id": instance_id,
                       "model_name_or_path": model, "model_patch": patch})
            if agent.returncode == 124:
                record["status"] = "agent_timed_out"
            elif agent.returncode:
                record["status"] = "agent_error"
            elif record.get("turns", 0) > max_turns:
                record["status"] = "turn_limit_exceeded"
            elif not patch:
                record["status"] = "empty_patch"
            else:
                script_path = attempt / "eval.sh"
                script_path.write_bytes(eval_script.encode("utf-8"))
                evaluated = _evaluate(image_id, attempt / "model.patch", script_path,
                                      attempt, eval_timeout, _acceptance_patterns(test_patch))
                record.update(evaluated)
                if agent.returncode and record["status"] == "resolved":
                    record["status"] = "agent_error"
        except (FactoryError, OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
            record["status"] = "error"
            record["error"] = str(exc)
        finally:
            if rollout.exists():
                if not rollout.resolve().is_relative_to(output):
                    raise FactoryError("Unsafe rollout cleanup path")
                _remove_rollout(rollout)
            write_json(attempt / "result.json", record)
            results.append(record)
    success = sum(item["status"] == "resolved" for item in results)
    def reported_total(key):
        values = [item[key] for item in results if key in item]
        return sum(values) if values else None
    cost_total = reported_total("cost_usd")
    summary = {"instance_id": instance_id, "model": model, "repetitions": repetitions,
               "resolved": success, "success_rate": success / repetitions,
               "total_agent_seconds": round(sum(item.get("agent_seconds", 0) for item in results), 3),
               "reported_input_tokens": reported_total("input_tokens"),
               "reported_output_tokens": reported_total("output_tokens"),
               "reported_cost_usd": round(cost_total, 6) if cost_total is not None else None,
               "results": results}
    write_json(output / "summary.json", summary)
    return summary
