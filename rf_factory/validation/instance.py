"""Existing pre-publication Docker control gates for each instance route."""


def real_controls_valid(noop, golden):
    return noop.returncode != 0 and golden.returncode == 0


def synthetic_compound_controls_valid(controls, spec_count):
    return not (
        controls["noop"].returncode == 0 or controls["gold"].returncode != 0
        or controls["gold_clean_apply"].returncode != 0
        or any(controls[f"partial_{i}"].returncode == 0
               or controls[f"partial_{i}_own"].returncode != 0
               or f"test_rf_migration_{1-i}" not in controls[f"partial_{i}"].stderr
               for i in range(spec_count))
    )


def historical_controls_valid(controls, strategy):
    return not (
        controls["noop"].returncode == 0 or controls["gold"].returncode != 0
        or controls["gold_clean_apply"].returncode != 0
        or strategy["noop_failure_marker"] not in controls["noop"].stderr
    )


def historical_compound_controls_valid(controls):
    return not (
        controls["noop"].returncode == 0 or
        controls["noop_0"].returncode == 0 or controls["noop_1"].returncode == 0 or
        controls["gold"].returncode != 0 or
        controls["gold_clean_apply"].returncode != 0 or
        controls["partial_0"].returncode == 0 or
        controls["partial_0_own"].returncode != 0 or
        "test_subtask_1_operations" not in controls["partial_0"].stderr or
        ("partial_1" in controls and (controls["partial_1"].returncode == 0 or
         controls["partial_1_own"].returncode != 0 or
         "test_subtask_0_operations" not in controls["partial_1"].stderr))
    )


def hybrid_controls_valid(controls, prefix_count):
    return not (
        controls["baseline"].returncode != 0 or controls["noop"].returncode == 0 or
        controls["gold"].returncode != 0 or
        controls["gold_clean_apply"].returncode != 0 or
        any(controls[f"partial_{i}"].returncode == 0 or
            controls[f"partial_{i}_own"].returncode != 0
            for i in range(prefix_count - 1)) or
        controls["partial_without_2"].returncode == 0 or
        controls["partial_without_2_own"].returncode != 0
    )
