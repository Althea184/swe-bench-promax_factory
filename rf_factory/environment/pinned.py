"""Build and verify the existing pinned Docker environments by route."""
from ..core.common import FactoryError, digest, write_json
from ..core.process import command, require


def _result(process):
    return {"returncode": process.returncode, "stdout": process.stdout,
            "stderr": process.stderr}


def prepare_real_environment(output, repo_url, base_commit, base_image, offline_build, test_argv):
    dockerfile = (f"FROM {base_image}\n"
                  "RUN command -v git || (apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*)\n"
                  "RUN rm -rf /testbed\n"
                  "COPY acquired/ /testbed/\nWORKDIR /testbed\n"
                  "RUN python -m pip install --no-cache-dir " +
                  ("--no-index --no-build-isolation " if offline_build else "") + "-e .\n"
                  "ENV PYTHONPATH=/testbed PIP_NO_INDEX=1 UV_OFFLINE=1\n")
    (output / "Dockerfile").write_text(dockerfile, encoding="utf-8")
    (output / ".dockerignore").write_text("*\n!Dockerfile\n!acquired/\n!acquired/**\n", encoding="utf-8")
    image_name = "rf-promax:" + digest(f"{repo_url}:{base_commit}:{base_image}:{offline_build}".encode())[:16]
    build = command(["docker", "build", "-t", image_name, str(output)], timeout=1800)
    (output / "docker-build.stdout.txt").write_text(build.stdout, encoding="utf-8")
    (output / "docker-build.stderr.txt").write_text(build.stderr, encoding="utf-8")
    require(build, "Docker image build")
    image_id = require(command(["docker", "image", "inspect", "--format", "{{.Id}}", image_name]),
                       "Docker image inspection")

    def run_script(script):
        return command(["docker", "run", "--rm", "--network", "none", image_name,
                        "bash", "-c", script], timeout=600)

    image_commit = require(run_script("cd /testbed && git rev-parse HEAD"), "Image commit check")
    if image_commit != base_commit:
        raise FactoryError("Docker image contains the wrong base commit")
    write_json(output / "environment.json", {"repo_url": repo_url,
               "image_name": image_name, "image_id": image_id,
               "base_image": base_image, "base_commit": base_commit,
               "working_dir": "/testbed", "offline_build": offline_build,
               "test_argv": test_argv})
    return image_name, image_id, run_script


def prepare_historical_environment(output, record, base, base_image, offline_build, test_argv):
    dockerfile = (f"FROM {base_image}\n"
                  "RUN command -v git || (apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*)\n"
                  "RUN rm -rf /testbed\nCOPY acquired/ /testbed/\nWORKDIR /testbed\n"
                  "RUN python -m pip install --no-cache-dir " +
                  ("--no-index --no-build-isolation " if offline_build else "") + "-e .\n"
                  "ENV PYTHONPATH=/testbed PIP_NO_INDEX=1 UV_OFFLINE=1\n")
    (output / "Dockerfile").write_text(dockerfile, encoding="utf-8")
    (output / ".dockerignore").write_text("*\n!Dockerfile\n!acquired/\n!acquired/**\n", encoding="utf-8")
    image_name = "rf-history:" + digest(f"{record['repo_url']}:{base}:{base_image}:{offline_build}".encode())[:16]
    build = command(["docker", "build", "-t", image_name, str(output)], timeout=1800)
    (output / "docker-build.stdout.txt").write_text(build.stdout, encoding="utf-8")
    (output / "docker-build.stderr.txt").write_text(build.stderr, encoding="utf-8")
    require(build, "History Docker image build")
    image_id = require(command(["docker", "image", "inspect", "--format", "{{.Id}}", image_name]),
                       "History image inspection")

    def run_script(script):
        return command(["docker", "run", "--rm", "--network", "none", image_name,
                        "bash", "-c", script], timeout=600)

    if require(run_script("cd /testbed && git rev-parse HEAD"), "History image commit") != base:
        raise FactoryError("History image contains the wrong base commit")
    if require(run_script("cd /testbed && git status --porcelain"), "History image clean tree"):
        raise FactoryError("History image worktree differs from the base commit")
    write_json(output / "environment.json", {"repo_url": record["repo_url"],
               "image_name": image_name, "image_id": image_id, "base_image": base_image,
               "base_commit": base, "working_dir": "/testbed", "offline_build": offline_build,
               "test_argv": test_argv})
    return image_name, image_id, run_script


def prepare_historical_compound_environment(output, acquired, repo_url, base, base_image, offline_build, test_argv):
    dockerfile = (f"FROM {base_image}\n"
        "RUN command -v git || (apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*)\n"
        "RUN rm -rf /testbed\nCOPY acquired/ /testbed/\nWORKDIR /testbed\n"
        "RUN python -m pip install --no-cache-dir " +
        ("--no-index --no-build-isolation " if offline_build else "") + "-e .\n"
        "ENV PYTHONPATH=/testbed PIP_NO_INDEX=1 UV_OFFLINE=1\n")
    (output / "Dockerfile").write_text(dockerfile, encoding="utf-8")
    (output / ".dockerignore").write_text("*\n!Dockerfile\n!acquired/\n!acquired/**\n", encoding="utf-8")
    require(command(["git", "checkout", "--detach", base], cwd=acquired),
            "Docker base checkout")
    image_name = "rf-history-compound:" + digest(
        f"{repo_url}:{base}:{base_image}:{offline_build}".encode())[:16]
    build = command(["docker", "build", "-t", image_name, str(output)], timeout=1800)
    write_json(output / "docker_build.json", _result(build))
    require(build, "Historical compound Docker build")
    image_id = require(command(["docker", "image", "inspect", "--format", "{{.Id}}",
                                image_name]), "Historical compound image inspection")
    def run_script(value):
        return command(["docker", "run", "--rm", "--network", "none", image_name,
                        "bash", "-c", value], timeout=600)
    if require(run_script("git rev-parse HEAD"), "Docker base commit") != base:
        raise FactoryError("Docker image has the wrong base revision")
    if require(run_script("git status --porcelain"), "Docker clean tree"):
        raise FactoryError("Docker image base tree is dirty")
    write_json(output / "environment.json", {"repo_url": repo_url,
        "base_commit": base, "image_name": image_name, "image_id": image_id,
        "base_image": base_image, "offline_build": offline_build,
        "working_dir": "/testbed", "test_argv": test_argv})
    return image_name, image_id, run_script


