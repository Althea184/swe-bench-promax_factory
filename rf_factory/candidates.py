"""Select discovered opportunities before task specification."""


def filter_and_rank(candidates, config):
    """Current policy is deterministic; later experiments can replace this stage."""
    eligible = [candidate for candidate in candidates if candidate.strategy == "api-migration-v1"]
    return sorted(eligible, key=lambda candidate: (candidate.path, candidate.start_line))[:config.max_candidates]
