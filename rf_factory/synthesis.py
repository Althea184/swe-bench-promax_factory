from pathlib import Path
from .common import digest
from .models import Candidate, Task


class TemplateSynthesizer:
    def synthesize(self, candidate: Candidate) -> Task:
        template = Path(__file__).with_name("prompts").joinpath("extract_helper.md").read_text(encoding="utf-8")
        statement = template.format(symbol=candidate.symbol, path=candidate.path,
                                    start_line=candidate.start_line, end_line=candidate.end_line, reason=candidate.reason)
        return Task(candidate, statement, (
            "Extract a cohesive private helper in the same module and call it from the target.",
            "Preserve public signature and observable behavior, including exceptions and side effects.",
            "Do not modify tests, dependencies or unrelated files.",
        ), template_sha256=digest(template.encode()))
