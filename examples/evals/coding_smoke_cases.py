from dataclasses import dataclass

from cairn.evals import (
    CheckResult,
    EvalCase,
    EvalCheck,
    EvalSuite,
    EvalSuiteCase,
    FileContainsCheck,
    FileContentEqualsCheck,
    FileExistsCheck,
    FileNotContainsCheck,
)
from cairn.workspace.workspace import Workspace

SmokeCase = tuple[EvalCase, tuple[EvalCheck, ...]]


@dataclass(slots=True)
class FileImportFromCheck:
    """Match complete import prefixes using the existing streaming text check."""

    path: str
    module: str
    name: str = "file_import_from"

    async def evaluate(self, workspace: Workspace) -> CheckResult:
        for module in (f".{self.module}", f"src.{self.module}", self.module):
            check = FileContainsCheck(self.path, f"from {module} import")
            if (await check.evaluate(workspace)).passed:
                return CheckResult(name=self.name, passed=True)
        return CheckResult(
            name=self.name,
            passed=False,
            message=f"Expected {self.path} to import from {self.module}, "
            f".{self.module}, or src.{self.module}",
        )


def coding_smoke_cases() -> tuple[SmokeCase, ...]:
    range_utils = (
        "def inclusive_count(start: int, end: int) -> int:\n"
        '    """Count integers in [start, end], where start <= end."""\n'
        "    return end - start\n"
    )
    retry = (
        "def retry(operation):\n"
        "    for attempt in range(3):\n"
        "        try:\n"
        "            return operation()\n"
        "        except OSError:\n"
        "            continue\n"
        '    raise RuntimeError("operation failed after retries")\n'
    )
    user_service = (
        "def username_for_lookup(raw: str) -> str:\n"
        "    return raw.strip().lower()\n"
        "\n"
        "\n"
        "def username_for_storage(raw: str) -> str:\n"
        "    return raw.strip().lower()\n"
    )
    identity = (
        "def normalize_username(value: str) -> str:\n"
        "    return value\n"
        "\n"
        "\n"
        "def format_user_id(value: int) -> str:\n"
        '    return f"user-{value:04d}"\n'
    )
    slug = (
        "def normalize_slug(value: str) -> str:\n"
        '    return value.strip().lower().replace(" ", "-")\n'
    )

    return (
        (
            EvalCase(
                name="single-file-bug-fix",
                prompt=(
                    "Inspect src/range_utils.py. Fix inclusive_count for start <= end: "
                    "[4, 4] contains one integer and [2, 5] contains four. Preserve its "
                    "public signature and docstring, and make the smallest coherent "
                    "change to the return expression."
                ),
                files={"src/range_utils.py": range_utils},
            ),
            (
                FileContentEqualsCheck(
                    "src/range_utils.py",
                    range_utils.replace("return end - start", "return end - start + 1"),
                ),
            ),
        ),
        (
            EvalCase(
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
                    "src/settings.py": (
                        '"""Shared defaults for service helpers."""\n\n'
                        "DEFAULT_TIMEOUT_SECONDS = 5\n"
                    ),
                    "src/retry.py": retry,
                },
            ),
            (
                FileContainsCheck("src/settings.py", "DEFAULT_RETRY_LIMIT = 3"),
                FileContainsCheck("src/settings.py", "DEFAULT_TIMEOUT_SECONDS = 5"),
                FileImportFromCheck("src/retry.py", "settings"),
                FileContainsCheck("src/retry.py", "range(DEFAULT_RETRY_LIMIT)"),
                FileNotContainsCheck("src/retry.py", "range(3)"),
            ),
        ),
        (
            EvalCase(
                name="create-and-wire-helper",
                prompt=(
                    "Inspect src/user_service.py. Extract the duplicated username "
                    "normalization into src/text_utils.py as normalize_username(value: "
                    "str) -> str. It must strip surrounding whitespace and lowercase "
                    "the value. Directly import this helper without an alias and use it "
                    "in both existing functions, preserving their public signatures "
                    "and behavior. Remove inline normalization from user_service.py."
                ),
                files={"src/__init__.py": "", "src/user_service.py": user_service},
            ),
            (
                FileExistsCheck("src/text_utils.py"),
                FileContainsCheck("src/text_utils.py", "def normalize_username("),
                FileContainsCheck("src/text_utils.py", ".strip()"),
                FileContainsCheck("src/text_utils.py", ".lower()"),
                FileImportFromCheck("src/user_service.py", "text_utils"),
                FileContainsCheck("src/user_service.py", "normalize_username(raw)"),
                FileContainsCheck("src/user_service.py", "def username_for_lookup("),
                FileContainsCheck("src/user_service.py", "def username_for_storage("),
                FileNotContainsCheck("src/user_service.py", ".strip()"),
                FileNotContainsCheck("src/user_service.py", ".lower()"),
            ),
        ),
        (
            EvalCase(
                name="minimal-change",
                prompt=(
                    "Inspect src/identity.py. Update only the return expression in "
                    "normalize_username so it strips surrounding whitespace and "
                    "lowercases the username. Preserve format_user_id and all unrelated "
                    "code exactly; make the smallest coherent change."
                ),
                files={"src/identity.py": identity},
            ),
            (
                FileContentEqualsCheck(
                    "src/identity.py",
                    identity.replace(
                        "    return value\n", "    return value.strip().lower()\n", 1
                    ),
                ),
            ),
        ),
        (
            EvalCase(
                name="no-op-correct-code",
                prompt=(
                    "Inspect src/slug.py. Ensure normalize_slug trims surrounding "
                    "whitespace, lowercases the value, and replaces spaces with "
                    "hyphens. If the implementation already satisfies these "
                    "requirements, leave the file unchanged and do not make "
                    "unnecessary edits."
                ),
                files={"src/slug.py": slug},
            ),
            (FileContentEqualsCheck("src/slug.py", slug),),
        ),
    )


def coding_smoke_suite() -> EvalSuite:
    """The original five prompts, fixtures and checks in their original order."""
    return EvalSuite(
        name="coding-smoke",
        cases=tuple(
            EvalSuiteCase(case, checks) for case, checks in coding_smoke_cases()
        ),
    )
