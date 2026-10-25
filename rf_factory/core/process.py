"""Run checked subprocesses with the existing Factory error contract."""
import subprocess
from .common import FactoryError


def command(argv, *, cwd=None, timeout=900):
    try:
        return subprocess.run(argv, cwd=cwd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise FactoryError(f"{argv[0]} execution failed: {exc}") from exc


def require(result, operation):
    if result.returncode:
        raise FactoryError(f"{operation} failed ({result.returncode}): {result.stderr[-2000:]}")
    return result.stdout.strip()


