"""Existing Hybrid Docker control serialization and patch application."""
import base64


def _result(process):
    return {"returncode": process.returncode, "stdout": process.stdout,
            "stderr": process.stderr}


def _with_patch(patch, script):
    encoded = base64.b64encode(patch.encode()).decode()
    return ("cd /testbed && echo '" + encoded + "' | base64 -d > /tmp/rf-change.patch && "
            "git apply --check /tmp/rf-change.patch && git apply /tmp/rf-change.patch || exit 1\n" +
            script)


