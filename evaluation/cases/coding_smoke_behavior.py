"""Versioned behavioral acceptance for the five coding-smoke prompts.

The original ``coding_smoke.py`` corpus keeps its exact-content oracles unchanged
and remains the frozen reference (``coding-smoke``). This module adds a *separate,
versioned* behavioral acceptance set built on those same five prompts and
fixtures. A changed acceptance oracle needs a new version, per
[AGENTS.md](../../../AGENTS.md).

How it judges
-------------
Each case supplies a **trusted probe program** that records raw observations with
``obs(name, value)`` and a host-side ``expected`` map. The probe runs isolated; the
*verdict* is computed on the host by comparing observations to expectations (see
[`cairn.runtime.evals.behavior`](../../../src/cairn/runtime/evals/behavior.py)).
A candidate that does not complete (``os._exit``/``sys.exit``/crash) yields no
complete observation record and fails; the expected values never enter the
sandbox, so they cannot be read or forged.

``graded-behavior-v2`` — the probe programs, expected observations, prompts,
fixtures and protected-file set are all part of the versioned contract.
"""

import hashlib
import json
from dataclasses import dataclass

from cairn.runtime.evals.behavior import BehaviorCheck, Contains
from cairn.runtime.evals.checks import (
    FileContentEqualsCheck,
    FileNotContainsCheck,
)
from cairn.runtime.evals.models import EvalCase, EvalCheck, EvalSuite, EvalSuiteCase

#: The frozen version string for this behavior set's probes, expectations,
#: prompts, boundaries and protected-file set. Bump it whenever any of those change.
BEHAVIOR_DATASET_VERSION: str = "graded-behavior-v2"

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


# --- Trusted probe programs (record observations only; no verdict) -----------

_RANGE_PROBE = """\
import importlib.util
spec = importlib.util.spec_from_file_location("range_utils", "src/range_utils.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
f = mod.inclusive_count
obs("f44", f(4, 4))
obs("f25", f(2, 5))
obs("f00", f(0, 0))
obs("fneg", f(-3, 2))
obs("f77", f(7, 7))
obs("doc", f.__doc__ or "")
"""
_RANGE_EXPECTED = {
    "f44": 1,
    "f25": 4,
    "f00": 1,
    "fneg": 6,
    "f77": 1,
    "doc": Contains("start"),
}

_RETRY_PROBE = """\
import importlib
mod = importlib.import_module("src.retry")
f = mod.retry
n = [0]
def ok():
    n[0] += 1
    return "value"
obs("ok_result", f(ok))
obs("ok_calls", n[0])
flaky = [0]
def flaky_ok():
    flaky[0] += 1
    if flaky[0] < 2:
        raise OSError("transient")
    return "recovered"
obs("flaky_result", f(flaky_ok))
obs("flaky_calls", flaky[0])
def always_fail():
    raise OSError("down")
try:
    f(always_fail)
except RuntimeError:
    obs("always", "RuntimeError")
else:
    obs("always", "no-raise")
obs("has_limit", "DEFAULT_RETRY_LIMIT" in open("src/retry.py", encoding="utf-8").read())
"""
_RETRY_EXPECTED = {
    "ok_result": "value",
    "ok_calls": 1,
    "flaky_result": "recovered",
    "flaky_calls": 2,
    "always": "RuntimeError",
    "has_limit": True,
}

_USER_SERVICE_PROBE = """\
import importlib.util
import text_utils
spec = importlib.util.spec_from_file_location("user_service", "src/user_service.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
for fn in (mod.username_for_lookup, mod.username_for_storage):
    name = fn.__name__
    obs(name + "_a", fn("  Alice "))
    obs(name + "_b", fn("BOB"))
    obs(name + "_c", fn("  mIxEd "))
obs("helper", text_utils.normalize_username("  QR "))
"""
_USER_SERVICE_EXPECTED = {
    "username_for_lookup_a": "alice",
    "username_for_lookup_b": "bob",
    "username_for_lookup_c": "mixed",
    "username_for_storage_a": "alice",
    "username_for_storage_b": "bob",
    "username_for_storage_c": "mixed",
    "helper": "qr",
}

_IDENTITY_PROBE = """\
import importlib.util
spec = importlib.util.spec_from_file_location("identity", "src/identity.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
obs("n1", mod.normalize_username("  Alice "))
obs("n2", mod.normalize_username("BOB"))
obs("n3", mod.normalize_username(""))
obs("id7", mod.format_user_id(7))
obs("id1234", mod.format_user_id(1234))
"""
_IDENTITY_EXPECTED = {
    "n1": "alice",
    "n2": "bob",
    "n3": "",
    "id7": "user-0007",
    "id1234": "user-1234",
}

_SLUG_PROBE = """\
import importlib.util
spec = importlib.util.spec_from_file_location("slug", "src/slug.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
f = mod.normalize_slug
obs("s1", f("  Hello World "))
obs("s2", f("A  B"))
obs("s3", f(""))
"""
_SLUG_EXPECTED = {"s1": "hello-world", "s2": "a--b", "s3": ""}


@dataclass(frozen=True, slots=True)
class BehaviorCorpusCase:
    """One case: its prompt fixture, probe program, expectations, protected files."""

    case: EvalCase
    program: str
    expected: dict[str, object]
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
        program=_RANGE_PROBE,
        expected=dict(_RANGE_EXPECTED),
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
        program=_RETRY_PROBE,
        expected=dict(_RETRY_EXPECTED),
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
        program=_USER_SERVICE_PROBE,
        expected=dict(_USER_SERVICE_EXPECTED),
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
        program=_IDENTITY_PROBE,
        expected=dict(_IDENTITY_EXPECTED),
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
        program=_SLUG_PROBE,
        expected=dict(_SLUG_EXPECTED),
        protected={},
        checks=(),
    )
    return (single, multi, create, minimal, noop)


def _assemble(spec: BehaviorCorpusCase, version: str) -> EvalSuiteCase:
    checks: list[EvalCheck] = [
        BehaviorCheck(f"{spec.case.name}:behavior", spec.program, spec.expected)
    ]
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
    """Exact original contents per case name for the files it must not rewrite."""
    return {
        spec.case.name: {
            path: contents
            for path, contents in spec.protected.items()
            if contents is not None
        }
        for spec in behavior_cases()
    }


def behavior_dataset_digest() -> str:
    """Stable SHA256 over the versioned prompts, probes, expectations and checks."""
    payload = []
    for spec in behavior_cases():
        payload.append(
            {
                "case": spec.case.model_dump(),
                "program": spec.program,
                "expected": {
                    key: (value.substring if isinstance(value, Contains) else value)
                    for key, value in spec.expected.items()
                },
                "expected_contains": sorted(
                    key
                    for key, value in spec.expected.items()
                    if isinstance(value, Contains)
                ),
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
