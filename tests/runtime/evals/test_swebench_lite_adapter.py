"""Offline format validation for the SWE-bench Lite thin adapter.

No Docker, no ``swebench`` package, no network: this verifies the manifest
contract, the official prediction JSONL shape and the subset summary parsing.
Real scoring runs in the official harness and is not exercised here.
"""

import json
from pathlib import Path

import pytest
from evaluation.benchmarks import swebench_lite as lite

SUBSET = lite.SUBSET


def test_subset_is_fixed_size_and_unique() -> None:
    ids = lite.instance_ids()
    assert 10 <= len(ids) <= 20
    assert len(set(ids)) == len(ids)
    assert lite.SUBSET_VERSION == "swebench-lite-subset-v1"
    # Every instance names a repo and a 40-char base commit.
    for item in SUBSET:
        assert "/" in item.repo
        assert len(item.base_commit) == 40
        assert item.instance_id.startswith(item.repo.split("/")[1] + "__") or (
            "__" in item.instance_id
        )
        # Version is either verified (a string) or explicitly unknown (None).
        assert item.version is None or isinstance(item.version, str)


def test_subset_digest_is_pinned() -> None:
    assert lite.subset_digest() == lite.subset_digest()  # deterministic
    # A silent change to any instance, repo, base commit or version must change
    # this digest and therefore require a new SUBSET_VERSION.
    assert (
        lite.subset_digest()
        == "fa280cd98c798652c66f2c61a9ac569d231709133e6f565178660823061147e1"
    )


def test_prediction_line_matches_official_shape() -> None:
    prediction = lite.prediction_for_diff(
        "astropy__astropy-12907", "cairn-test-model", "diff --git a/x b/x\n"
    )
    line = json.loads(prediction.to_jsonl())
    assert set(line) == {"instance_id", "model_name_or_path", "model_patch"}
    assert line["instance_id"] == "astropy__astropy-12907"
    assert line["model_patch"].startswith("diff --git")


def test_empty_diff_is_a_legal_prediction() -> None:
    prediction = lite.prediction_for_diff("django__django-11001", "m", "")
    assert prediction.model_patch == ""
    lite.validate_prediction_line(json.loads(prediction.to_jsonl()))


def test_out_of_subset_instance_is_refused() -> None:
    with pytest.raises(ValueError, match="not in the frozen subset"):
        lite.prediction_for_diff("sympy__sympy-99999", "m", "diff")


def test_malformed_prediction_lines_are_refused() -> None:
    good = {
        "instance_id": "django__django-11001",
        "model_name_or_path": "m",
        "model_patch": "",
    }
    with pytest.raises(ValueError, match="missing keys"):
        lite.validate_prediction_line({"instance_id": "x"})
    with pytest.raises(ValueError, match="unexpected keys"):
        lite.validate_prediction_line({**good, "extra": 1})
    with pytest.raises(ValueError, match="must be a string"):
        lite.validate_prediction_line({**good, "model_patch": 1})
    with pytest.raises(ValueError, match="nonempty"):
        lite.validate_prediction_line({**good, "instance_id": " "})
    lite.validate_prediction_line(good)


def test_write_predictions_is_one_json_line_each(tmp_path: Path) -> None:
    predictions = [
        lite.prediction_for_diff("django__django-11001", "m", "a"),
        lite.prediction_for_diff("sympy__sympy-11400", "m", ""),
    ]
    destination = tmp_path / "predictions.jsonl"
    lite.write_predictions(predictions, destination)
    lines = destination.read_text().splitlines()
    assert len(lines) == 2
    assert [json.loads(line)["instance_id"] for line in lines] == [
        "django__django-11001",
        "sympy__sympy-11400",
    ]


def test_summarize_results_restricts_to_subset() -> None:
    # A results.json scenario: two resolved, one unresolved, one empty patch,
    # one error, one out-of-subset id that must be ignored.
    results = {
        "resolved": ["astropy__astropy-12907", "django__django-11001"],
        "unresolved": ["sympy__sympy-11400"],
        "empty_patch_ids": ["sympy__sympy-11870"],
        "error_ids": ["django__django-10924"],
        "not_in_subset": ["django__django-1"],  # out-of-subset, ignored
        "unrelated": ["some__other-1"],
    }
    summary = lite.summarize_results(results)
    assert summary.total == len(SUBSET)
    assert summary.resolved == 2
    assert summary.unresolved == 1
    assert summary.empty_patches == 1
    assert summary.errors == 1
    assert summary.completed == 3  # resolved + unresolved
    assert summary.submitted == 5
    assert summary.incomplete == len(SUBSET) - 5
    # resolved / graded = 2 / 3
    assert summary.resolved_rate == pytest.approx(2 / 3)


def test_summarize_partial_results_does_not_fabricate() -> None:
    summary = lite.summarize_results({})
    assert summary.resolved == summary.unresolved == 0
    assert summary.incomplete == len(SUBSET)
    # Nothing graded => no rate, not zero.
    assert summary.resolved_rate is None


def test_summarize_rejects_non_list_results() -> None:
    with pytest.raises(ValueError, match="must be a list"):
        lite.summarize_results({"resolved": "not-a-list"})
