Refactor `{symbol}` in `{path}` (original lines {start_line}–{end_line}).
Extract one cohesive portion of its implementation into a meaningfully named private helper in the same module, and call that helper from `{symbol}`.
Preserve the public function name and signature, return values, exceptions, mutation and side-effect ordering. Keep existing tests unchanged. Do not add dependencies or change unrelated files.
The heuristic identified this target because: {reason}.
Acceptance requires an actual helper extraction and preserved observable behavior; passing the existing tests alone is insufficient to establish either.
