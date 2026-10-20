"""Package only tasks that pass both control evaluations."""
from .common import digest, dumps
from .models import Instance, Repository


class InstanceBuilder:
    def build(self, repo, spec, problem, oracle, patch_record, verifier_record, precheck, difficulty_metadata):
        portable = Repository(repo.source, "source", repo.content_sha256, repo.files)
        identity = digest(dumps([repo.content_sha256, spec, oracle, patch_record["patch_sha256"]], sort_keys=True).encode())[:24]
        return Instance("rf-" + identity, portable, spec, problem, oracle, patch_record,
                        verifier_record, precheck,
                        {"dockerfile": f"docker/{spec.candidate.candidate_id}/Dockerfile",
                         "python": ">=3.11", "dependencies": []}, difficulty_metadata)
