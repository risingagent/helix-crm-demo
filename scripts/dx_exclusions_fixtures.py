"""Per-patient fixture assertions for the Dx Exclusion test harness.

Mirrors `lab_recs_fixtures.py` — same FixtureCheck primitive, same helper
shape, different clinical assertions.

Schema-only patients (no domain-specific clinical assertions yet) are listed
in SCHEMA_ONLY_PATIENTS so the harness still records that it tested them.

Add fixtures over time — start with SYNTH-005 per spec 3.3.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Dict, List, Tuple


# ---------------------------------------------------------------------------
# FixtureCheck primitive (identical to lab_recs version — kept local for
# self-contained module; if we ever add a third clinical AI flow, refactor
# to a shared base.)
# ---------------------------------------------------------------------------

@dataclass
class FixtureCheck:
    """A single named clinical assertion against a Dx Exclusion payload."""

    name: str
    predicate: Callable[[dict], bool]
    detail: str  # human-readable description of what it checks

    def run(self, payload: dict) -> Tuple[bool, str]:
        try:
            ok = bool(self.predicate(payload))
        except Exception as exc:  # noqa: BLE001
            return False, "{}: predicate raised {}: {}".format(
                self.name, type(exc).__name__, exc
            )
        return ok, self.detail


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _exclusions(payload: dict) -> List[dict]:
    return payload.get("exclusions") or []


def _any_exclusion_matches(
    payload: dict,
    pattern: str,
    fields: Tuple[str, ...] = ("diagnosis", "considered_because", "exclusion_rationale"),
) -> bool:
    """Case-insensitive regex search across the named fields of every exclusion."""
    rx = re.compile(pattern, re.IGNORECASE)
    for ex in _exclusions(payload):
        for field in fields:
            value = ex.get(field) or ""
            if rx.search(value):
                return True
    return False


def _any_guideline_matches(payload: dict, pattern: str) -> bool:
    rx = re.compile(pattern, re.IGNORECASE)
    for ex in _exclusions(payload):
        if rx.search(ex.get("guideline_source") or ""):
            return True
    return False


def _uncertainty_mentions(payload: dict, pattern: str) -> bool:
    notes = payload.get("uncertainty_notes") or ""
    return bool(re.search(pattern, notes, re.IGNORECASE))


# ---------------------------------------------------------------------------
# Patient fixtures
# ---------------------------------------------------------------------------

# SYNTH-005 — Eleanor Hayes, geriatric multi-condition.
# A high-quality Dx Exclusion run for this patient should:
#   - exclude acute decompensated HF (HFpEF on therapy, NT-proBNP stable)
#   - exclude hypothyroidism as cause of cognitive decline (TSH normal)
#   - cite NICE NG106 (heart failure) AND NICE NG97 (dementia)
#   - flag the anticholinergic burden / tamsulosin duplicate in uncertainty_notes
_SYNTH_005_CHECKS = [
    FixtureCheck(
        name="hf_decompensation_excluded",
        predicate=lambda p: _any_exclusion_matches(
            p, r"decompensat|acute heart failure|acute hf|adhf"
        ),
        detail="At least one exclusion addresses acute decompensated heart failure.",
    ),
    FixtureCheck(
        name="hypothyroidism_excluded",
        predicate=lambda p: _any_exclusion_matches(p, r"hypothyroid|TSH|thyroid"),
        detail="At least one exclusion addresses hypothyroidism as a reversible cause.",
    ),
    FixtureCheck(
        name="hf_guideline_cited",
        predicate=lambda p: _any_guideline_matches(p, r"NG106"),
        detail="At least one exclusion cites NICE NG106 (chronic heart failure).",
    ),
    FixtureCheck(
        name="dementia_guideline_cited",
        predicate=lambda p: _any_guideline_matches(p, r"NG97"),
        detail="At least one exclusion cites NICE NG97 (dementia).",
    ),
    FixtureCheck(
        name="anticholinergic_or_polypharmacy_flagged",
        predicate=lambda p: _uncertainty_mentions(
            p, r"anticholinergic|tamsulosin|polypharmacy|duplicate"
        ),
        detail="uncertainty_notes flags anticholinergic burden / tamsulosin duplicate / polypharmacy.",
    ),
    FixtureCheck(
        name="depression_or_pseudodementia_flagged",
        predicate=lambda p: (
            _any_exclusion_matches(p, r"depression|pseudodementia|PHQ-?9|GDS")
            or _uncertainty_mentions(p, r"depression|pseudodementia|PHQ-?9|GDS")
        ),
        detail="Depression as cognitive co-contributor is addressed in exclusions or uncertainty_notes.",
    ),
]


# Map of patient_id -> list of FixtureCheck. Add more over time.
PATIENT_FIXTURES: Dict[str, List[FixtureCheck]] = {
    "SYNTH-005": _SYNTH_005_CHECKS,
}


# Patients exercised by the harness with schema-only validation (no clinical
# fixtures yet). SYNTH-001 is the healthy baseline — its exclusions array may
# be sparse or empty since there's nothing acute to rule out.
SCHEMA_ONLY_PATIENTS: List[str] = [
    "SYNTH-001",
    "SYNTH-002",
    "SYNTH-003",
    "SYNTH-004",
    "SYNTH-006",
    "SYNTH-007",
    "SYNTH-008",
    "SYNTH-009",
    "SYNTH-010",
]


# Patients allowed to have an empty uncertainty_notes string AND/OR a sparse
# exclusions array. SYNTH-001 is healthy with no presenting concerns —
# exclusions array can be empty or contain only screening-related entries.
PATIENTS_ALLOWED_EMPTY_UNCERTAINTY: List[str] = ["SYNTH-001"]
PATIENTS_ALLOWED_SPARSE_EXCLUSIONS: List[str] = ["SYNTH-001"]


def all_test_patients() -> List[str]:
    """Ordered list of every patient the harness should hit."""
    seen: List[str] = []
    for pid in SCHEMA_ONLY_PATIENTS + list(PATIENT_FIXTURES.keys()):
        if pid not in seen:
            seen.append(pid)
    seen.sort()
    return seen
