"""Run the unmodified ProMax evaluator on Windows with argv-based Docker dispatch.

The upstream runner constructs POSIX-quoted commands and uses shell=True. Windows
cmd.exe does not recognize the single quotes, so only its process launcher is
adapted here. All patch application, evaluation and scoring stay upstream.
"""
import argparse
import importlib.util
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path


def run_command(command, check=False, timeout=600):
    lexer = shlex.shlex(command, posix=True)
    lexer.whitespace_split = True
    lexer.escape = ""
    argv = list(lexer)
    print("execute:", command)
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        if check and result.returncode:
            print(f"Command failed with retcode {result.returncode}")
        return result, False
    except subprocess.TimeoutExpired as exc:
        stderr = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        stdout = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        return subprocess.CompletedProcess(argv, 124, stdout, stderr + f"\nTIMEOUT AFTER {timeout}s"), True


def create_temp_file(content, suffix="", prefix="tmp"):
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="",
                                     suffix=suffix, prefix=prefix, delete=False) as stream:
        stream.write(content)
        return stream.name


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--official-script", required=True, type=Path)
    args, remaining = parser.parse_known_args()
    spec = importlib.util.spec_from_file_location("promax_official_test_run", args.official_script)
    if spec is None or spec.loader is None:
        raise RuntimeError("Cannot load official evaluator")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.run_command = run_command
    module.create_temp_file = create_temp_file
    sys.argv = [str(args.official_script), *remaining]
    module.main()


if __name__ == "__main__":
    main()
