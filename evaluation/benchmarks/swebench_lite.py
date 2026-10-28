"""A fixed internal SWE-bench Lite subset: manifest, Cairn→prediction adapter.

SWE-bench grades a *prediction* against a task's own tests inside the official
Docker harness. This module is a **thin adapter**, not a re-implementation of
that harness:

* it pins a small, fixed internal subset (10-20 instances) of SWE-bench Lite by
  instance id, repo and base commit, with a stable digest;
* it converts a Cairn run's unified diff into one official prediction JSONL line
  (``instance_id``, ``model_name_or_path``, ``model_patch``);
* it parses the official ``results.json`` into subset counts, clearly labelled a
  **subset**, never a full leaderboard.

The official harness owns the environment, patch application and grading; this
module never applies a patch or runs a test itself. Everything here is offline
and dependency-free (no ``swebench``/``datasets`` import), so it is testable on
hosts without the Docker harness.

Authoritative contract:
https://www.swebench.com/SWE-bench/guides/evaluation/
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

#: SWE-bench's official prediction file is JSONL; each line holds exactly these
#: keys. See the evaluation guide linked above.
PREDICTION_KEYS: tuple[str, ...] = (
    "instance_id",
    "model_name_or_path",
    "model_patch",
)


@dataclass(frozen=True, slots=True)
class SubsetInstance:
    """One pinned SWE-bench Lite instance."""

    instance_id: str
    repo: str
    base_commit: str
    #: The dataset ``version`` field; ``None`` when not yet verified against the
    #: official dataset. Unknown stays null, never guessed.
    version: str | None = None


#: A fixed internal subset of SWE-bench Lite (12 instances). Chosen for a mix of
#: repositories and difficulty; the order is frozen so a run is reproducible.
#: Instance ids, repos and base commits come from the official
#: ``princeton-nlp/SWE-bench_Lite`` test split.
SUBSET: tuple[SubsetInstance, ...] = (
    SubsetInstance(
        "astropy__astropy-12907",
        "astropy/astropy",
        "d16bfe05a744909de4b27f5875fe0d4ed41ce607",
        "4.3",
    ),
    SubsetInstance(
        "astropy__astropy-14182",
        "astropy/astropy",
        "a5917978be39d13cd90b517e1de4e7a539ffaa48",
        "5.1",
    ),
    SubsetInstance(
        "astropy__astropy-14365",
        "astropy/astropy",
        "7269fa3e33e8d02485a647da91a5a2a60a06af61",
        "5.1",
    ),
    SubsetInstance(
        "astropy__astropy-14995",
        "astropy/astropy",
        "b16c7d12ccbc7b2d20364b89fb44285bcbfede54",
        "5.3",
    ),
    SubsetInstance(
        "astropy__astropy-6938",
        "astropy/astropy",
        "c76af9ed6bb89bfba45b9f5bc1e635188278e2fa",
        None,
    ),
    SubsetInstance(
        "astropy__astropy-7746",
        "astropy/astropy",
        "d5bd3f68bb6d5ce3a61bdce9883ee750d1afade5",
        None,
    ),
    SubsetInstance(
        "django__django-10914",
        "django/django",
        "e7fd69d051eaa67cb17f172a39b57253e9cb831a",
        None,
    ),
    SubsetInstance(
        "django__django-10924",
        "django/django",
        "bceadd2788dc2dad53eba0caae172bd8522fd483",
        "3.0",
    ),
    SubsetInstance(
        "django__django-11001",
        "django/django",
        "ef082ebb84f00e38af4e8880d04e8365c2766d34",
        "3.0",
    ),
    SubsetInstance(
        "sympy__sympy-11400",
        "sympy/sympy",
        "8dcb12a6cf500e8738d6729ab954a261758f49ca",
        "1.0",
    ),
    SubsetInstance(
        "sympy__sympy-11870",
        "sympy/sympy",
        "5c2e1f96a7ff562d4a778f4ca9ffc9c81557197e",
        "1.1",
    ),
    SubsetInstance(
        "sympy__sympy-11897",
        "sympy/sympy",
        "e2918c1205c47345eb73c9be68b14c0f15fdeb17",
        "1.0",
    ),
)

#: Frozen subset identity; a change to the subset needs a new version string.
SUBSET_VERSION: str = "swebench-lite-subset-v1"


def subset_digest() -> str:
    """Stable SHA256 over the pinned subset, so a change is visible."""
    payload = [
        {
            "instance_id": item.instance_id,
            "repo": item.repo,
            "base_commit": item.base_commit,
            "version": item.version,
        }
        for item in SUBSET
    ]
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def instance_ids() -> tuple[str, ...]:
    return tuple(item.instance_id for item in SUBSET)


@dataclass(frozen=True, slots=True)
class Prediction:
    """One official-format prediction line."""

    instance_id: str
    model_name_or_path: str
    model_patch: str

    def to_jsonl(self) -> str:
        return json.dumps(
            {
                "instance_id": self.instance_id,
                "model_name_or_path": self.model_name_or_path,
                "model_patch": self.model_patch,
            },
            separators=(",", ":"),
        )


def validate_prediction_line(line: Mapping[str, object]) -> None:
    """Raise ``ValueError`` unless ``line`` matches the official shape."""
    missing = [key for key in PREDICTION_KEYS if key not in line]
    if missing:
        raise ValueError(f"prediction missing keys: {missing}")
    extra = sorted(set(line) - set(PREDICTION_KEYS))
    if extra:
        raise ValueError(f"prediction has unexpected keys: {extra}")
    for key in PREDICTION_KEYS:
        if not isinstance(line[key], str):
            raise ValueError(f"prediction field {key!r} must be a string")
    instance_id = line["instance_id"]
    if not isinstance(instance_id, str) or not instance_id.strip():
        raise ValueError("instance_id must be nonempty")


def prediction_for_diff(
    instance_id: str, model_name_or_path: str, patch: str
) -> Prediction:
    """Build a prediction from a Cairn unified diff.

    An empty diff is a legal, scored prediction (the harness counts it as
    "empty patch"); the adapter does not invent a change.
    """
    if instance_id not in instance_ids():
        raise ValueError(f"{instance_id} is not in the frozen subset")
    if not model_name_or_path.strip():
        raise ValueError("model_name_or_path must be nonempty")
    prediction = Prediction(instance_id, model_name_or_path, patch)
    validate_prediction_line(
        {
            "instance_id": prediction.instance_id,
            "model_name_or_path": prediction.model_name_or_path,
            "model_patch": prediction.model_patch,
        }
    )
    return prediction


def write_predictions(predictions: list[Prediction], destination: Path) -> None:
    """Write official JSONL, one line per prediction, preserving order."""
    text = "".join(prediction.to_jsonl() + "\n" for prediction in predictions)
    destination.write_text(text, encoding="utf-8")


@dataclass(frozen=True, slots=True)
class SubsetSummary:
    """Counts parsed from the official ``results.json``, restricted to the subset.

    These are subset counts, not a benchmark score and not a leaderboard entry.
    """

    total: int
    submitted: int
    completed: int
    resolved: int
    unresolved: int
    empty_patches: int
    errors: int
    #: Instances in the subset with no prediction at all.
    incomplete: int

    @property
    def resolved_rate(self) -> float | None:
        """Resolved / graded, or ``None`` when nothing was graded."""
        graded = self.resolved + self.unresolved
        return self.resolved / graded if graded else None


def _subset_keys(results: Mapping[str, object]) -> set[str]:
    return {item.instance_id for item in SUBSET}


def summarize_results(results: Mapping[str, object]) -> SubsetSummary:
    """Parse the official harness ``results.json`` for this subset only.

    The official file holds ``resolved``, ``unresolved``, ``error_ids`` and
    ``empty_patch_ids`` (lists of instance ids). Unknown or absent keys are
    treated as an empty list rather than an error, so a partial run is readable.
    """

    def ids(key: str) -> list[str]:
        value = results.get(key, [])
        if not isinstance(value, list):
            raise ValueError(f"results field {key!r} must be a list")
        return [item for item in value if isinstance(item, str)]

    subset = _subset_keys(results)
    resolved = [item for item in ids("resolved") if item in subset]
    unresolved = [item for item in ids("unresolved") if item in subset]
    errors = [item for item in ids("error_ids") if item in subset]
    empty = [item for item in ids("empty_patch_ids") if item in subset]
    graded_ids = set(resolved) | set(unresolved) | set(errors) | set(empty)
    return SubsetSummary(
        total=len(SUBSET),
        submitted=len(graded_ids),
        completed=len(resolved) + len(unresolved),
        resolved=len(resolved),
        unresolved=len(unresolved),
        empty_patches=len(empty),
        errors=len(errors),
        incomplete=len(SUBSET) - len(graded_ids),
    )
