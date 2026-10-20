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
        if (spec.entities.get("current_api") != spec.entities.get("target_api")
                and spec.entities.get("current_api", "") in text
                and spec.entities.get("target_api", "") in text):
            issues.append("exact API transformation")
        return {"passed": not issues, "issues": issues, "abstraction_level": "medium"}
