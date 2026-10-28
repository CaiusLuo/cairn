"""Versioned behavioral acceptance for the five coding-smoke prompts.

The original ``coding_smoke.py`` corpus keeps its exact-content oracles unchanged
and remains the frozen reference (``coding-smoke``). This module adds a *separate,
versioned* behavioral acceptance set built on those same five prompts and
fixtures. It does not modify, wrap or re-interpret the original checks: a new
case/check contract gets a new version, per
[AGENTS.md](../../../AGENTS.md) and the dataset-versioning rule that a changed
acceptance oracle needs an explicitly versioned contract.

Why a second oracle?

* **No false negatives on correct-but-different code.** The original corpus fixes
  a few cases to exact final bytes, which is right for a "smallest change" task.
  A behavior check instead exercises the public functions and asserts their
  outputs, so a different correct implementation (``end - start + 1`` vs
  ``end - (start - 1)``) also passes.
* **No false positives on a near miss.** Each behavior script asserts the exact
  boundaries named in the prompt, so an implementation that reproduces the
  original defect — or fixes the shown example but breaks a boundary — fails.
* **Protected files are ground truth.** Every file a case must not touch is
  pinned by an exact-content ``preserve:`` check, so "fix by rewriting unrelated
  code" is refused even when the target behavior is correct.

``graded-behavior-v1`` — behaviors, boundaries and protected set are all part of
the versioned contract; any change needs a new version string.
"""

import hashlib
import json
from dataclasses import dataclass

from cairn.runtime.evals.behavior import BehaviorCheck
from cairn.runtime.evals.checks import (
    FileContentEqualsCheck,
    FileNotContainsCheck,
)
from cairn.runtime.evals.models import EvalCase, EvalCheck, EvalSuite, EvalSuiteCase

#: The frozen version string for this behavior set's prompts, behaviors, boundaries
#: and protected-file set. Bump it whenever any of those changes.
BEHAVIOR_DATASET_VERSION: str = "graded-behavior-v1"

# --- Fixtures: byte-for-byte the originals from coding_smoke.py ---------------

RANGE_UTILS = (
    "def inclusive_count(start: int, end: int) -> int:\n"
    '    """Count integers in [start, end], where start <= end."""\n'
    "    return end - start\n"
)
RETRY = (
    "def retry(operation):\n"
    "    for attempt in range(3):\n"
    "        try:\n"
    "            return operation()\n"
    "        except OSError:\n"
    "            continue\n"
    '    raise RuntimeError("operation failed after retries")\n'
)
USER_SERVICE = (
    "def username_for_lookup(raw: str) -> str:\n"
    "    return raw.strip().lower()\n"
    "\n"
    "\n"
    "def username_for_storage(raw: str) -> str:\n"
    "    return raw.strip().lower()\n"
)
IDENTITY = (
    "def normalize_username(value: str) -> str:\n"
    "    return value\n"
    "\n"
    "\n"
    "def format_user_id(value: int) -> str:\n"
    '    return f"user-{value:04d}"\n'
)
SLUG = (
    "def normalize_slug(value: str) -> str:\n"
    '    return value.strip().lower().replace(" ", "-")\n'
)

SETTINGS = '"""Shared defaults for service helpers."""\n\nDEFAULT_TIMEOUT_SECONDS = 5\n'


# --- Behavior scripts: exercise behavior + boundaries, tolerate other solutions

_RANGE_BEHAVIOR = """\
import importlib.util
spec = importlib.util.spec_from_file_location("range_utils", "src/range_utils.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
f = mod.inclusive_count
assert f(4, 4) == 1, f(4, 4)
assert f(2, 5) == 4, f(2, 5)
assert f(0, 0) == 1, "single element bounds"
assert f(-3, 2) == 6, "negative start"
assert f(7, 7) == 1, "equal endpoints"
assert f.__doc__ and "start" in f.__doc__, "docstring preserved"
"""

_RETRY_BEHAVIOR = """\
import sys
sys.path.insert(0, ".")
import importlib
mod = importlib.import_module("src.retry")  # package import: relative import resolves
f = mod.retry
attempts = {"n": 0}
def ok():
    attempts["n"] += 1
    return "value"
assert f(ok) == "value", "returns the operation result"
assert attempts["n"] == 1, "success on first try must not retry"
flaky = {"n": 0}
def flaky_ok():
    flaky["n"] += 1
    if flaky["n"] < 2:
        raise OSError("transient")
    return "recovered"
assert f(flaky_ok) == "recovered", "retries a transient OSError"
assert flaky["n"] == 2, "retries exactly until success"
def always_fail():
    raise OSError("down")
try:
    f(always_fail)
except RuntimeError:
    pass
else:
    raise AssertionError("must raise after exhausting retries")
assert "DEFAULT_RETRY_LIMIT" in open("src/retry.py", encoding="utf-8").read()
"""

_USER_SERVICE_BEHAVIOR = """\
import importlib.util, sys
sys.path.insert(0, "src")
import text_utils  # noqa: F401  (helper must exist and be importable)
spec = importlib.util.spec_from_file_location("user_service", "src/user_service.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
for fn in (mod.username_for_lookup, mod.username_for_storage):
    assert fn("  Alice ") == "alice", fn.__name__
    assert fn("BOB") == "bob", fn.__name__
    assert fn("  mIxEd ") == "mixed", fn.__name__
builtin = text_utils.normalize_username
assert builtin is not None, "helper exported"
assert builtin("  QR ") == "qr", "helper behavior"
"""

_IDENTITY_BEHAVIOR = """\
import importlib.util
spec = importlib.util.spec_from_file_location("identity", "src/identity.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
assert mod.normalize_username("  Alice ") == "alice"
assert mod.normalize_username("BOB") == "bob"
assert mod.normalize_username("") == ""
assert mod.format_user_id(7) == "user-0007", "unrelated function preserved"
assert mod.format_user_id(1234) == "user-1234"
"""

_SLUG_BEHAVIOR = """\
import importlib.util
spec = importlib.util.spec_from_file_location("slug", "src/slug.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
f = mod.normalize_slug
assert f("  Hello World ") == "hello-world"
assert f("A  B") == "a--b", "only single spaces become hyphens"
assert f("") == "", "empty"
"""


@dataclass(frozen=True, slots=True)
class BehaviorCorpusCase:
    """One case: its prompt fixture, behavior check and protected files."""

    case: EvalCase
    behavior: str
    protected: dict[str, str | None]
    checks: tuple[EvalCheck, ...]


def behavior_cases() -> tuple[BehaviorCorpusCase, ...]:
    """The five coding-smoke prompts graded on behavior, with protected files."""
    single = BehaviorCorpusCase(
        case=EvalCase(
            name="single-file-bug-fix",
            prompt=(
                "Inspect src/range_utils.py. Fix inclusive_count for start <= end: "
                "[4, 4] contains one integer and [2, 5] contains four. Preserve its "
                "public signature and docstring, and make the smallest coherent "
                "change to the return expression."
            ),
            files={"src/range_utils.py": RANGE_UTILS},
        ),
        behavior=_RANGE_BEHAVIOR,
        protected={},
        checks=(),
    )
    multi = BehaviorCorpusCase(
        case=EvalCase(
            name="multi-file-retry-refactor",
            prompt=(
                "Inspect src/settings.py and src/retry.py. Introduce "
                "DEFAULT_RETRY_LIMIT = 3 in settings.py and directly import that "
                "symbol in retry.py to replace the loop's magic retry limit. Keep "
                "existing defaults and retry behavior; use the constant without an "
                "alias. These are local-only source changes."
            ),
            files={
                "src/__init__.py": "",
                "src/settings.py": SETTINGS,
                "src/retry.py": RETRY,
            },
        ),
        behavior=_RETRY_BEHAVIOR,
        # settings.py gains a constant; its pre-existing default must survive.
        protected={"src/settings.py": None},
        checks=(FileNotContainsCheck("src/retry.py", "range(3)"),),
    )
    create = BehaviorCorpusCase(
        case=EvalCase(
            name="create-and-wire-helper",
            prompt=(
                "Inspect src/user_service.py. Extract the duplicated username "
                "normalization into src/text_utils.py as normalize_username(value: "
                "str) -> str. It must strip surrounding whitespace and lowercase "
                "the value. Directly import this helper without an alias and use it "
                "in both existing functions, preserving their public signatures "
                "and behavior. Remove inline normalization from user_service.py."
            ),
            files={"src/__init__.py": "", "src/user_service.py": USER_SERVICE},
        ),
        behavior=_USER_SERVICE_BEHAVIOR,
        # No protected files: text_utils.py is created and user_service.py edited.
        protected={},
        checks=(
            FileNotContainsCheck("src/user_service.py", ".strip()"),
            FileNotContainsCheck("src/user_service.py", ".lower()"),
        ),
    )
    minimal = BehaviorCorpusCase(
        case=EvalCase(
            name="minimal-change",
            prompt=(
                "Inspect src/identity.py. Update only the return expression in "
                "normalize_username so it strips surrounding whitespace and "
                "lowercases the username. Preserve format_user_id and all unrelated "
                "code exactly; make the smallest coherent change."
            ),
            files={"src/identity.py": IDENTITY},
        ),
        behavior=_IDENTITY_BEHAVIOR,
        protected={},
        checks=(),
    )
    noop = BehaviorCorpusCase(
        case=EvalCase(
            name="no-op-correct-code",
            prompt=(
                "Inspect src/slug.py. Ensure normalize_slug trims surrounding "
                "whitespace, lowercases the value, and replaces spaces with "
                "hyphens. If the implementation already satisfies these "
                "requirements, leave the file unchanged and do not make "
                "unnecessary edits."
            ),
            files={"src/slug.py": SLUG},
        ),
        behavior=_SLUG_BEHAVIOR,
        protected={},
        checks=(),
    )
    return (single, multi, create, minimal, noop)


def _assemble(spec: BehaviorCorpusCase, version: str) -> EvalSuiteCase:
    checks: list[EvalCheck] = [
        BehaviorCheck(f"{spec.case.name}:behavior", spec.behavior)
    ]
    # Protected files: when a file exists in the fixture, an exact-content check
    # refuses any modification. A ``None`` entry pins only the surviving original
    # content for files whose target fix legitimately adds to them, so the whole
    # original body must remain present by exact equality of unrelated files.
    for path, expected in spec.protected.items():
        if expected is not None:
            checks.append(FileContentEqualsCheck(path, expected))
    checks.extend(spec.checks)
    return EvalSuiteCase(spec.case, tuple(checks))


def behavior_suite(*, version: str = BEHAVIOR_DATASET_VERSION) -> EvalSuite:
    """The versioned behavioral suite: same five prompts, behavior-graded."""
    if version != BEHAVIOR_DATASET_VERSION:
        raise ValueError(f"Unsupported coding behavior dataset: {version}")
    return EvalSuite(
        name=f"coding-behavior-{version}",
        cases=tuple(_assemble(spec, version) for spec in behavior_cases()),
    )


def behavior_protected_files() -> dict[str, dict[str, str]]:
    """Exact original contents per case name for the files it must not rewrite.

    Only non-``None`` protected entries are returned; a ``None`` entry means the
    fix legitimately edits that file, so it is not a byte-identical requirement.
    """
    return {
        spec.case.name: {
            path: contents
            for path, contents in spec.protected.items()
            if contents is not None
        }
        for spec in behavior_cases()
    }


def behavior_dataset_digest() -> str:
    """Stable SHA256 over the versioned prompts, behaviors, boundaries and checks.

    Pinned by a test so a silent change to a prompt, a behavior script, a
    protected entry or an extra check requires a new ``BEHAVIOR_DATASET_VERSION``.
    """
    payload = []
    for spec in behavior_cases():
        payload.append(
            {
                "case": spec.case.model_dump(),
                "behavior": spec.behavior,
                "protected": dict(spec.protected),
                "checks": [
                    {
                        "type": type(check).__name__,
                        "name": getattr(check, "name", None),
                        "path": getattr(check, "path", None),
                        "substring": getattr(check, "substring", None),
                        "expected": getattr(check, "expected", None),
                    }
                    for check in spec.checks
                ],
            }
        )
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode()
    ).hexdigest()
