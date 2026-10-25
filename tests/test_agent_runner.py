"""Agent Runner contracts: isolated rollouts, patch collection and evaluation handoff."""
import json
import subprocess
import sys

from rf_factory.agents import runner as agent_runner


def test_codex_cli_usage_fallback_ignores_other_output():
    events = "\n".join((
        "not json",
        json.dumps({"type": "turn.completed", "usage": {
            "input_tokens": 100, "cached_input_tokens": 60, "output_tokens": 5}}),
        json.dumps({"type": "turn.completed", "usage": {
            "input_tokens": 40, "output_tokens": 3}}),
    ))
    assert agent_runner._codex_cli_usage(events) == {
        "input_tokens": 140, "output_tokens": 8}


def checked(*argv, cwd=None):
    subprocess.run(argv, cwd=cwd, check=True, capture_output=True)


def published_instance(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    checked("git", "init", "-q", str(source))
    checked("git", "config", "user.name", "Factory Test", cwd=source)
    checked("git", "config", "user.email", "factory@example.test", cwd=source)
    (source / "module.py").write_text("value = 1\n", encoding="utf-8")
    checked("git", "add", ".", cwd=source)
    checked("git", "commit", "-qm", "base", cwd=source)
    base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
    run = tmp_path / "instance"
    run.mkdir()
    row = {"instance_id": "owner__repo-c_1234567-example",
           "repository": {"base_commit": base, "image_id": "sha256:" + "1" * 64}}
    (run / "instances.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
    (run / "eval.json").write_text(json.dumps({row["instance_id"]: {"eval_script": "echo tested"}}),
                                    encoding="utf-8")
    (run / "test.patch").write_text("", encoding="utf-8")

    def rollout(_run, _id, output):
        output.mkdir()
        checked("git", "clone", "-q", str(source), str(output / "repository"))
        (output / "task.md").write_text("Set value to 2.\n", encoding="utf-8")

    monkeypatch.setattr(agent_runner, "prepare_rollout", rollout)
    return run, row["instance_id"]


def test_runner_collects_each_clean_patch_and_metrics(tmp_path, monkeypatch):
    run, instance = published_instance(tmp_path, monkeypatch)
    script = tmp_path / "solver.py"
    script.write_text("import json, os\nfrom pathlib import Path\n"
                      "Path('module.py').write_text('value = 2\\n')\n"
                      "Path(os.environ['RF_RESULT_FILE']).write_text(json.dumps("
                      "{'turns': 3, 'input_tokens': 10, 'output_tokens': 5, 'cost_usd': 0.01}))\n",
                      encoding="utf-8")
    evaluated = []

    def evaluate(_image, patch, _script, _attempt, _timeout, _patterns):
        evaluated.append(patch.read_text(encoding="utf-8"))
        return {"status": "resolved", "returncode": 0}

    monkeypatch.setattr(agent_runner, "_evaluate", evaluate)
    output = tmp_path / "results"
    summary = agent_runner.run_agent(run=run, instance_id=instance, output=output,
                                      agent_command=f'"{sys.executable}" "{script}"',
                                      model="test-agent", repetitions=2, max_turns=5)
    assert summary["resolved"] == 2 and summary["success_rate"] == 1
    assert summary["reported_input_tokens"] == 20
    assert summary["reported_cost_usd"] == 0.02
    assert len(evaluated) == 2 and all("+value = 2" in patch for patch in evaluated)
    for index in range(2):
        attempt = output / "attempts" / f"{index:03d}"
        result = json.loads((attempt / "result.json").read_text(encoding="utf-8"))
        assert result["turns"] == 3 and result["input_tokens"] == 10
        assert b"\r\n" not in (attempt / "model.patch").read_bytes()
        assert not (attempt / "rollout").exists()


def test_runner_rejects_test_file_changes(tmp_path, monkeypatch):
    run, instance = published_instance(tmp_path, monkeypatch)
    script = tmp_path / "bad_solver.py"
    script.write_text("from pathlib import Path\n"
                      "Path('tests').mkdir()\n"
                      "Path('tests/test_rf_migration.py').write_text('pass\\n')\n",
                      encoding="utf-8")
    monkeypatch.setattr(agent_runner, "_evaluate", lambda *_: (_ for _ in ()).throw(
        AssertionError("Protected changes must not reach Docker")))
    output = tmp_path / "results"
    summary = agent_runner.run_agent(run=run, instance_id=instance, output=output,
                                      agent_command=f'"{sys.executable}" "{script}"')
    assert summary["results"][0]["status"] == "error"
    assert "protected test files" in summary["results"][0]["error"]


def test_compound_acceptance_patterns_are_controller_only():
    patch = "+++ b/rf_acceptance/test_rf_migration_0.py\n+++ b/rf_acceptance/test_rf_migration_1.py\n"
    assert agent_runner._acceptance_patterns(patch) == [
        "test_rf_migration_0.py", "test_rf_migration_1.py"]


def test_historical_compound_acceptance_patterns_are_reported():
    patch = "+++ b/rf_acceptance/test_rf_history_0.py\n+++ b/rf_acceptance/test_rf_history_1.py\n"
    assert agent_runner._acceptance_patterns(patch) == [
        "test_rf_history_0.py", "test_rf_history_1.py"]


def test_agent_cli_error_is_not_reported_as_empty_patch(tmp_path, monkeypatch):
    run, instance = published_instance(tmp_path, monkeypatch)
    summary = agent_runner.run_agent(run=run, instance_id=instance,
        output=tmp_path / "results", agent_command=f'"{sys.executable}" -c "import sys; sys.exit(2)"')
    assert summary["results"][0]["status"] == "agent_error"
    assert summary["results"][0]["agent_returncode"] == 2


def test_reported_turn_limit_blocks_evaluation(tmp_path, monkeypatch):
    run, instance = published_instance(tmp_path, monkeypatch)
    script = tmp_path / "over_budget.py"
    script.write_text("import json, os\nfrom pathlib import Path\n"
                      "Path('module.py').write_text('value = 2\\n')\n"
                      "Path(os.environ['RF_RESULT_FILE']).write_text(json.dumps({'turns': 6}))\n",
                      encoding="utf-8")
    monkeypatch.setattr(agent_runner, "_evaluate", lambda *_: (_ for _ in ()).throw(
        AssertionError("Over-budget run must not reach Docker")))
    summary = agent_runner.run_agent(run=run, instance_id=instance, output=tmp_path / "results",
                                      agent_command=f'"{sys.executable}" "{script}"', max_turns=5)
    assert summary["results"][0]["status"] == "turn_limit_exceeded"
