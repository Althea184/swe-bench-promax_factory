"""Solver-facing wording from TaskSpec, independently of the gold patch."""
import re
from pathlib import Path
from .common import FactoryError
from .models import ProblemStatement


class ProblemGenerator:
    def generate(self, spec, config):
        if config.abstraction_level != "medium":
            raise ValueError("Only medium abstraction is implemented")
        text = spec.goal.rstrip(".") + ". " + " ".join(spec.behavioral_invariants)
        if spec.compatibility_requirements:
            text += " " + " ".join(spec.compatibility_requirements)
        quality = self.check(text, spec)
        if not quality["passed"]:
            raise FactoryError("Problem statement quality check failed: " + ", ".join(quality["issues"]))
        return ProblemStatement(text, "medium", "task-spec-template-v1", quality)

    def generate_compound(self, spec, config):
        """One solver-facing objective for the two private subtask contracts."""
        if config.abstraction_level != "medium":
            raise ValueError("Only medium abstraction is implemented")
        invariants = dict.fromkeys(value.replace("this API", "both APIs")
                                   for task in spec.subtasks
                                   for value in task.behavioral_invariants)
        text = spec.goal.rstrip(".") + ". " + " ".join(invariants)
        quality = self.check(text, spec)
        if not quality["passed"]:
            raise FactoryError("Problem statement quality check failed: " + ", ".join(quality["issues"]))
        return ProblemStatement(text, "medium", "compound-task-spec-template-v1", quality)

    def generate_historical(self, spec):
        """Use the mined PR intent, retaining its controller-side evidence separately."""
        text = (spec.goal.rstrip(".") + ". " + " ".join(spec.behavioral_invariants) +
                " " + " ".join(spec.compatibility_requirements)).strip()
        quality = self.check(text, spec)
        if not quality["passed"] or any(value in text for value in ("_TrimmedRelease", "singledispatch")):
            raise FactoryError("Historical problem statement leaks implementation detail or is too vague")
        return ProblemStatement(text, "medium", "historical-intent-template-v1", quality)

    def generate_compound_historical(self, spec):
        """Present one goal and both independent requirements without provenance details."""
        requirements = dict.fromkeys(value for task in spec.subtasks
                                     for value in task.structural_requirements)
        invariants = dict.fromkeys(value for task in spec.subtasks
                                   for value in (*task.behavioral_invariants,
                                                 *task.compatibility_requirements))
        text = " ".join((spec.goal.rstrip(".") + ".", *requirements, *invariants))
        quality = self.check(text, spec)
        if not quality["passed"]:
            raise FactoryError("Historical compound problem statement quality check failed: " +
                               ", ".join(quality["issues"]))
        return ProblemStatement(text, "medium", "historical-compound-intent-v1", quality)

    @staticmethod
    def check(text, spec):
        issues = []
        if len(text.split()) < 18 or not spec.goal.strip():
            issues.append("too vague")
        if re.search(r"\b(?:line \d+|first |then |next |finally )\b", text, re.I):
            issues.append("implementation steps or line numbers")
        paths = [path for path in spec.scope if Path(path).name in text]
        if paths:
            issues.append("implementation file paths")
        entities = getattr(spec, "entities", {})
        if (entities.get("current_api") != entities.get("target_api")
                and entities.get("current_api", "") in text
                and entities.get("target_api", "") in text):
            issues.append("exact API transformation")
        return {"passed": not issues, "issues": issues, "abstraction_level": "medium"}
