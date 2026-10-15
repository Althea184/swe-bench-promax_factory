"""Serial orchestration with replaceable stage objects and preserved evidence."""
import logging
import platform
from pathlib import Path
from .common import FactoryError, dumps, export_jsonl, write_json
from .repository import RepositoryPreparer
from .discovery import LongFunctionDiscovery
from .synthesis import TemplateSynthesizer
from .oracle import BaselineOracle
from .validation import TaskValidator
from .packaging import InstancePackager
from .contracts import Preparer, Discovery, Synthesizer, Oracle, Validator, Packager

LOG = logging.getLogger(__name__)


class Pipeline:
    def __init__(self, *, preparer: Preparer | None = None, discovery: Discovery | None = None,
                 synthesizer: Synthesizer | None = None, oracle: Oracle | None = None,
                 validator: Validator | None = None, packager: Packager | None = None):
        self.preparer = preparer or RepositoryPreparer()
        self.discovery = discovery or LongFunctionDiscovery()
        self.synthesizer = synthesizer or TemplateSynthesizer()
        self.oracle = oracle or BaselineOracle()
        self.validator = validator or TaskValidator()
        self.packager = packager or InstancePackager()

    def run(self, config, output: Path):
        output = output.resolve()
        if output.is_relative_to(config.source.resolve()):
            raise FactoryError("Output must be outside the source repository")
        output.mkdir(parents=True, exist_ok=False)
        stage = "preparation"

        def event(name, **details):
            nonlocal stage
            stage = name
            LOG.info("%s %s", name, details)
            with (output / "events.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(dumps({"stage": name, **details}) + "\n")

        try:
            write_json(output / "config.json", config)
            write_json(output / "runtime.json", {"python": platform.python_version(), "platform": platform.platform(), "factory": "0.1.0"})
            event("preparation")
            repo = self.preparer.prepare(config.source, output / "source")
            write_json(output / "repository.json", repo)
            event("discovery")
            candidates, diagnostics = self.discovery.discover(repo, config)
            write_json(output / "candidates.json", candidates)
            write_json(output / "discovery_diagnostics.json", diagnostics)
            tasks = []
            event("synthesis")
            for candidate in candidates:
                tasks.append(self.synthesizer.synthesize(candidate))
            write_json(output / "tasks.json", tasks)
            event("oracle_preparation")
            plan = self.oracle.prepare(repo, config)
            write_json(output / "oracle_plan.json", plan)
            event("validation")
            baseline = self.oracle.run(repo, plan, output / "baseline")
            validations = [self.validator.validate(repo, task, baseline, config) for task in tasks]
            write_json(output / "validation.json", [dict(candidate_id=t.candidate.candidate_id, **v.__dict__) for t, v in zip(tasks, validations)])
            event("packaging")
            instances = [self.packager.package(repo, t, plan, v) for t, v in zip(tasks, validations) if v.passed]
            write_json(output / "instances.json", instances)
            event("export")
            export_jsonl(output / "instances.jsonl", instances)
            summary = {"status": "completed" if instances else "no_valid_instances", "candidates": len(candidates),
                       "exported": len(instances), "rejected": sum(not v.passed for v in validations),
                       "output": str(output / "instances.jsonl"), "behavioral_equivalence": "not_proven",
                       "model_called": False, "docker_executed": False}
            event("complete", **summary)
            write_json(output / "summary.json", summary)
            return summary
        except Exception as exc:
            write_json(output / "summary.json", {"status": "failed", "stage": stage, "error": str(exc), "type": type(exc).__name__})
            event(stage, status="failed", error=str(exc))
            raise
