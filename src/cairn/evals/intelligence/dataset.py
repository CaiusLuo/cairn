"""Repo-intelligence-v1: fixed prompts, fixtures, facts and independent checks."""

import hashlib
import json
from dataclasses import asdict

from cairn.evals.checks import FileContentEqualsCheck
from cairn.evals.intelligence.checks import BehaviorCheck
from cairn.evals.intelligence.fixtures import IntelligenceCase, MemoryFixture
from cairn.evals.models import EvalCase, EvalCheck, EvalSuiteCase

VERSION = "repo-intelligence-v1"


def repository_files() -> dict[str, str]:
    files = {
        "README.md": (
            "Forge is a small Python service. Source lives in forge.\n"
            "Verification: python -m unittest discover -s checks -p 'check_*.py'\n"
            "Regression checks use unittest.TestCase in checks/check_*.py.\n"
            "Account canonicalization and quote contracts are shared across consumers.\n"
        ),
        "forge/__init__.py": "",
        "forge/pricing/__init__.py": "",
        "forge/pricing/calculator.py": (
            "def catalog_subtotal(rows):\n"
            "    return round(sum(price * quantity for price, quantity in rows) * 1.08, 2)\n"
        ),
        "forge/shipping/__init__.py": "",
        "forge/shipping/policy.py": (
            "def delivery_fee(subtotal, expedited=False):\n"
            "    return (5 if subtotal < 50 else 0) + (7 if expedited else 0)\n"
        ),
        "forge/checkout/__init__.py": "",
        "forge/checkout/service.py": (
            "from forge.pricing.calculator import catalog_subtotal\n"
            "from forge.shipping.policy import delivery_fee\n\n"
            "def checkout_total(rows, expedited=False):\n"
            "    subtotal = catalog_subtotal(rows)\n"
            "    return round(subtotal + delivery_fee(subtotal, expedited), 2)\n"
        ),
        "forge/text/__init__.py": "",
        "forge/text/slugs.py": (
            "def canonical_label(value):\n    return value.lower().replace(' ', '-')\n"
        ),
        "forge/api/__init__.py": "",
        "forge/api/tags.py": (
            "from forge.text.slugs import canonical_label\n\n"
            "def render_tag(value):\n"
            "    return 'tag:' + canonical_label(value)\n"
        ),
        "docs/text-contract.txt": "Labels trim and collapse ASCII whitespace and use Unicode casefold.\n",
        "forge/storage/__init__.py": "",
        "forge/storage/identities.py": (
            "def normalize_account(raw):\n    return raw.strip().casefold()\n"
        ),
        "forge/cache/__init__.py": "",
        "forge/cache/keys.py": (
            "from forge.storage.identities import normalize_account\n\n"
            "def cache_identity(raw):\n"
            "    return 'account:' + raw.lower()\n"
        ),
        "forge/audit/__init__.py": "",
        "forge/audit/records.py": (
            "from forge.storage.identities import normalize_account\n\n"
            "def audit_identity(raw):\n"
            "    return {'account': raw.strip().lower()}\n"
        ),
        "checks/check_account.py": (
            "import unittest\nfrom forge.storage.identities import normalize_account\n\n"
            "class AccountCase(unittest.TestCase):\n"
            "    def test_existing(self):\n"
            "        self.assertEqual(normalize_account(' A '), 'a')\n"
        ),
        "forge/routing/__init__.py": "",
        "forge/routing/limits.py": "MAX_BATCH = 50\n",
        "forge/routing/chunks.py": (
            "from forge.routing.limits import MAX_BATCH\n\n"
            "def split_batches(values):\n"
            "    return [values[i:i + 50] for i in range(0, len(values), 50)]\n"
        ),
        "forge/telemetry/__init__.py": "",
        "forge/telemetry/reduce.py": (
            "def median_delay(samples):\n"
            "    values = sorted(samples)\n"
            "    return values[len(values) // 2]\n"
        ),
    }
    for name in (
        "media",
        "archive",
        "capacity",
        "addresses",
        "profiles",
        "inventory",
        "labels",
        "receipts",
    ):
        files[f"forge/{name}.py"] = (
            f"def describe_{name}(value):\n    return str(value)\n"
        )
    return files


def dataset() -> tuple[IntelligenceCase, ...]:
    base = repository_files()
    account_fact = MemoryFixture(
        "account-contract",
        "Account canonicalization is supplied by the shared normalize_account export.",
        ("forge/storage/identities.py",),
        True,
    )
    specifications = (
        (
            "quote-dependencies",
            "Fix the quote contract across the catalog and checkout consumers. catalog_subtotal must expose "
            "pre-tax merchandise value. checkout_total must charge 8% tax on merchandise only, then the existing "
            "delivery fee determined by the pre-tax subtotal; retain the expedited surcharge and public signatures.",
            (
                "forge/pricing/calculator.py",
                "forge/checkout/service.py",
                "forge/shipping/policy.py",
            ),
            "from forge.pricing.calculator import catalog_subtotal\n"
            "from forge.checkout.service import checkout_total\n"
            "assert catalog_subtotal([(10, 2)]) == 20\n"
            "assert checkout_total([(10, 2)]) == 26.6\n"
            "assert checkout_total([(49, 1)]) == 57.92\n"
            "assert checkout_total([(50, 1)], True) == 61.0\n"
            "assert checkout_total([]) == 5\n",
            (
                MemoryFixture(
                    "quote-layout",
                    "catalog_subtotal is exported by pricing; checkout_total depends on pricing and shipping modules.",
                    (
                        "forge/pricing/calculator.py",
                        "forge/checkout/service.py",
                        "forge/shipping/policy.py",
                    ),
                    True,
                ),
            ),
            (),
        ),
        (
            "find-label-normalizer",
            "Find the normalization used by render_tag. Canonical labels must trim surrounding ASCII whitespace, "
            "collapse interior ASCII whitespace to one hyphen, and use Unicode casefold. Preserve render_tag's prefix and signature.",
            ("forge/text/slugs.py", "forge/api/tags.py"),
            "from forge.api.tags import render_tag\n"
            "from forge.text.slugs import canonical_label\n"
            "assert render_tag('  Straße\\t A  ') == 'tag:strasse-a'\n"
            "assert canonical_label('a\\n b') == 'a-b'\n"
            "assert canonical_label('') == ''\n",
            (
                MemoryFixture(
                    "tag-layout",
                    "render_tag imports canonical_label from the text module; label contract is recorded in docs/text-contract.txt.",
                    (
                        "forge/api/tags.py",
                        "forge/text/slugs.py",
                        "docs/text-contract.txt",
                    ),
                    True,
                ),
            ),
            (),
        ),
        (
            "shared-account-contract",
            "Repair cache_identity and audit_identity so account identifiers follow the established shared "
            "canonicalization contract, including surrounding whitespace and Unicode casing. Keep their return "
            "shapes and the shared normalizer's public signature and existing behavior.",
            (
                "forge/cache/keys.py",
                "forge/audit/records.py",
                "forge/storage/identities.py",
            ),
            "import inspect\nfrom forge.cache.keys import cache_identity\nfrom forge.audit.records import audit_identity\n"
            "from forge.storage.identities import normalize_account\n"
            "assert cache_identity(' Straße ') == 'account:strasse'\n"
            "assert audit_identity(' Straße ') == {'account': 'strasse'}\n"
            "assert normalize_account(' A ') == 'a'\n"
            "assert list(inspect.signature(normalize_account).parameters) == ['raw']\n",
            (account_fact,),
            (),
        ),
        (
            "testing-convention",
            "Account normalization must reject blank input with ValueError while preserving valid identifiers. "
            "Add an executable regression for whitespace-only input using this repository's established testing convention.",
            ("forge/storage/identities.py", "README.md", "checks/check_account.py"),
            "import sys, unittest\nfrom forge.storage import identities\n"
            "normalize_account = identities.normalize_account\n"
            "assert normalize_account(' Straße ') == 'strasse'\n"
            "try: normalize_account(' \\t ')\nexcept ValueError: pass\nelse: raise AssertionError('blank accepted')\n"
            "suite = unittest.TestLoader().discover('checks', pattern='check_*.py')\n"
            "assert suite.countTestCases() >= 2\n"
            "assert unittest.TextTestRunner().run(suite).wasSuccessful()\n"
            "def permissive(raw):\n    return '' if not raw.strip() else normalize_account(raw)\n"
            "identities.normalize_account = permissive\n"
            "for name, module in list(sys.modules.items()):\n"
            "    if name.startswith('check_') and module is not None:\n"
            "        for key, value in list(vars(module).items()):\n"
            "            if value is normalize_account: setattr(module, key, permissive)\n"
            "mutant = unittest.TestLoader().discover('checks', pattern='check_*.py')\n"
            "assert not unittest.TextTestRunner().run(mutant).wasSuccessful()\n",
            (
                MemoryFixture(
                    "testing-convention",
                    "Account regression verification uses python -m unittest discover -s checks -p 'check_*.py'; regressions use unittest.TestCase.",
                    ("README.md", "checks/check_account.py"),
                    True,
                ),
            ),
            (),
        ),
        (
            "stale-batch-history",
            "Repair split_batches to respect the current configured batch limit. Preserve every input item and "
            "order, handle empty input, and follow configuration rather than a historical batch-size constant.",
            ("forge/routing/chunks.py", "forge/routing/limits.py"),
            "from forge.routing import chunks\n"
            "values = list(range(41))\n"
            "batches = chunks.split_batches(values)\nassert [len(b) for b in batches] == [20, 20, 1]\n"
            "assert sum(batches, []) == values\nassert chunks.split_batches([]) == []\n"
            "chunks.MAX_BATCH = 7\nassert [len(b) for b in chunks.split_batches(list(range(15)))] == [7, 7, 1]\n",
            (
                MemoryFixture(
                    "historical-batch",
                    "The batch limit is MAX_BATCH = 50 in the routing configuration.",
                    ("forge/routing/limits.py",),
                    True,
                ),
                MemoryFixture(
                    "incomplete-batch",
                    "Batch routing configuration needs a complete dependency review before reuse.",
                    ("forge/routing/limits.py",),
                    True,
                    complete=False,
                ),
            ),
            (("forge/routing/limits.py", "MAX_BATCH = 20\n"),),
        ),
        (
            "unrelated-memory-control",
            "Repair median_delay: empty samples return 0, odd-sized samples return the middle value, and "
            "even-sized samples return the arithmetic mean of their two middle values. Preserve input samples.",
            ("forge/telemetry/reduce.py",),
            "from forge.telemetry.reduce import median_delay\n"
            "assert median_delay([]) == 0\nassert median_delay([9, 1, 3]) == 3\n"
            "assert median_delay([8, 2]) == 5\n"
            "values = [5, 1, 2, 7]\nassert median_delay(values) == 3.5\nassert values == [5, 1, 2, 7]\n",
            (
                MemoryFixture(
                    account_fact.name,
                    account_fact.statement,
                    account_fact.evidence_files,
                    False,
                ),
            ),
            (),
        ),
    )
    cases = []
    for name, prompt, relevant, script, facts, current in specifications:
        checks: list[EvalCheck] = [BehaviorCheck(f"{name}:behavior", script)]
        # Unrelated modules are immutable ground truth, not model self-reports.
        checks.extend(
            FileContentEqualsCheck(path, text, name=f"preserve:{path}")
            for path, text in base.items()
            if path not in relevant and path != "checks/check_account.py"
        )
        cases.append(
            IntelligenceCase(
                EvalSuiteCase(
                    EvalCase(name=name, prompt=prompt, files=dict(base)), tuple(checks)
                ),
                facts,
                relevant,
                current,
            )
        )
    return tuple(cases)


def dataset_digest(cases: tuple[IntelligenceCase, ...]) -> str:
    data = []
    for spec in cases:
        data.append(
            {
                "case": spec.item.case.model_dump(),
                "checks": [
                    asdict(check)
                    for check in spec.item.checks
                    if isinstance(check, (BehaviorCheck, FileContentEqualsCheck))
                ],
                "memories": [asdict(fact) for fact in spec.memories],
                "relevant": spec.relevant_files,
                "current": spec.current_files,
            }
        )
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, default=list).encode()
    ).hexdigest()
