from .models import Instance, Repository
from .common import digest, dumps


class InstancePackager:
    def package(self, repo, task, plan, validation):
        # Portable run-relative snapshot; identity changes with source, task or oracle plan.
        portable = Repository(repo.source, "source", repo.content_sha256, repo.files)
        identity = digest(dumps([repo.content_sha256, task, plan], sort_keys=True).encode())[:24]
        return Instance("rf-" + identity, portable, task, plan, validation)
