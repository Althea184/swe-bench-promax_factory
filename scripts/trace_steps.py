"""Summarize observable Codex actions from one or more --json traces."""
import argparse
import json
from collections import Counter
from pathlib import Path


def summarize(path: Path) -> dict:
    data = path.read_bytes()
    encoding = "utf-16" if data.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
    events = []
    invalid = []
    for line_number, line in enumerate(data.decode(encoding).splitlines(), 1):
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError as exc:
            invalid.append({"line": line_number, "error": exc.msg, "column": exc.colno})
    completed = [event.get("item", {}) for event in events if event.get("type") == "item.completed"]
    types = Counter(item.get("type") for item in completed)
    turns = [event for event in events if event.get("type") == "turn.completed"]
    return {
        "trace": str(path),
        "completed_turns": len(turns),
        "completed_commands": types["command_execution"],
        "completed_file_changes": types["file_change"],
        "observed_tool_steps": types["command_execution"] + types["file_change"],
        "completed_item_types": dict(types),
        "usage": [turn.get("usage", {}) for turn in turns],
        "invalid_jsonl_lines": invalid,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("traces", type=Path, nargs="+")
    args = parser.parse_args()
    print(json.dumps([summarize(path) for path in args.traces], indent=2))


if __name__ == "__main__":
    main()
