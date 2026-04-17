"""Deterministic source hierarchy and source-selection logic.

`resolve_statement_facts` already picks one candidate per metric using the
manifest's priority_rank. This module wraps that decision in a richer object
so the analyst output can explain *why* a source was chosen and what backup
sources exist. It never invents or guesses.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from app.models.canonical import SourceCitation
from app.models.mapping import ResolvedMetricValue
from app.models.source import EvidenceRef, SourceDocument, SourceManifest


# ---------------------------------------------------------------------------
# Source family detection + human-friendly priority reasons
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceFamily:
    """One layer of the source hierarchy."""

    key: str
    label: str
    rank: int  # lower = higher priority


FAMILY_QOE = SourceFamily("qoe", "QoE package / official financial package", 1)
FAMILY_MONTHLY_FS = SourceFamily("monthly_fs", "Monthly P&L / formal management accounts", 2)
FAMILY_AUDITED_FS = SourceFamily("audited_fs", "Audited financial statements", 2)
FAMILY_BILLING = SourceFamily("billing", "Billing / subscription export", 3)
FAMILY_KPI = SourceFamily("kpi", "KPI summary / operating KPIs", 4)
FAMILY_OTHER = SourceFamily("other", "Supporting export", 5)


_FAMILIES_BY_KEY = {
    f.key: f
    for f in (FAMILY_QOE, FAMILY_MONTHLY_FS, FAMILY_AUDITED_FS, FAMILY_BILLING, FAMILY_KPI, FAMILY_OTHER)
}


def _detect_family(document: SourceDocument) -> SourceFamily:
    """Map a SourceDocument to its source-hierarchy family."""

    role = (document.document_role or "").lower()
    name = (document.file_name or "").lower()
    if role == "qoe" or "qoe" in name:
        return FAMILY_QOE
    if role == "audited_fs":
        return FAMILY_AUDITED_FS
    if role == "monthly_fs" or "monthly" in name or "p&l" in name or "pnl" in name:
        return FAMILY_MONTHLY_FS
    if "billing" in name or "subscription" in name:
        return FAMILY_BILLING
    if "kpi" in name or "operating" in name:
        return FAMILY_KPI
    return FAMILY_OTHER


# ---------------------------------------------------------------------------
# Metric priority families
# ---------------------------------------------------------------------------


FINANCIAL_METRICS = {"revenue", "cogs", "gross_profit", "operating_expenses", "ebitda"}
OPERATIONAL_METRICS = {"headcount"}
RATIO_METRICS = {"gross_margin_pct", "ebitda_margin_pct"}


FINANCIAL_PRIORITY = [FAMILY_QOE, FAMILY_MONTHLY_FS, FAMILY_AUDITED_FS, FAMILY_BILLING, FAMILY_KPI, FAMILY_OTHER]
OPERATIONAL_PRIORITY = [FAMILY_KPI, FAMILY_MONTHLY_FS, FAMILY_QOE, FAMILY_BILLING, FAMILY_OTHER]


def _preferred_families(metric_key: str) -> list[SourceFamily]:
    if metric_key in OPERATIONAL_METRICS:
        return OPERATIONAL_PRIORITY
    return FINANCIAL_PRIORITY


# ---------------------------------------------------------------------------
# Citation construction
# ---------------------------------------------------------------------------


def _evidence_to_citation(evidence: EvidenceRef) -> SourceCitation:
    return SourceCitation(
        file=evidence.file_name,
        tab=evidence.sheet_name or evidence.section_name,
        range=evidence.cell_range,
        source_id=evidence.source_id,
    )


def _dedupe_citations(citations: list[SourceCitation]) -> list[SourceCitation]:
    """Collapse duplicate (file, tab, range) tuples."""

    seen: set[tuple[Optional[str], Optional[str], Optional[str]]] = set()
    unique: list[SourceCitation] = []
    for citation in citations:
        key = (citation.file, citation.tab, citation.range)
        if key in seen:
            continue
        seen.add(key)
        unique.append(citation)
    return unique


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


@dataclass
class SourceSelection:
    """Concrete decision about which source provided a metric's value."""

    selected: Optional[SourceCitation]
    backups: list[SourceCitation]
    priority_reason: str
    selected_family: Optional[SourceFamily]


def select_source(
    metric_key: str,
    metric: Optional[ResolvedMetricValue],
    manifest: SourceManifest,
) -> SourceSelection:
    """Pick the strongest source for a metric and explain why.

    `metric` carries evidence_refs from the normalize step. This function only
    ranks those evidence refs — it never invents a source. If `metric` is None
    or carries no evidence, returns an empty selection (missing-source case is
    the caller's responsibility to surface as an exception).
    """

    if metric is None or not metric.evidence_refs:
        return SourceSelection(
            selected=None,
            backups=[],
            priority_reason="No source evidence attached to this metric.",
            selected_family=None,
        )

    family_by_source: dict[str, SourceFamily] = {
        doc.source_id: _detect_family(doc) for doc in manifest.documents
    }
    preferred = _preferred_families(metric_key)
    preferred_rank = {family.key: index for index, family in enumerate(preferred)}

    # Rank each evidence ref by (family preference, then its document rank).
    def _rank(evidence: EvidenceRef) -> tuple[int, int]:
        family = family_by_source.get(evidence.source_id, FAMILY_OTHER)
        family_slot = preferred_rank.get(family.key, len(preferred))
        return (family_slot, family.rank)

    sorted_evidence = sorted(metric.evidence_refs, key=_rank)
    selected_evidence = sorted_evidence[0]
    selected_family = family_by_source.get(selected_evidence.source_id, FAMILY_OTHER)

    citations = [_evidence_to_citation(evidence) for evidence in sorted_evidence]
    citations = _dedupe_citations(citations)
    selected_citation = citations[0]
    backup_citations = citations[1:]

    if metric_key in RATIO_METRICS:
        priority_reason = (
            "Ratios are always recomputed from deterministic inputs; the cited file is where the base inputs came from."
        )
    elif metric_key in OPERATIONAL_METRICS:
        priority_reason = (
            f"Headcount is taken from the {selected_family.label} because that layer is the authoritative operating source."
        )
    else:
        priority_reason = (
            f"Selected the {selected_family.label} as the highest-ranked financial source available in this upload."
        )

    return SourceSelection(
        selected=selected_citation,
        backups=backup_citations,
        priority_reason=priority_reason,
        selected_family=selected_family,
    )
