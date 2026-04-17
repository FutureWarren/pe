# AGENTS.md

## Project Purpose

Build a local pilot for private equity databook workflow automation that takes a sample data room and outputs one high-quality, source-linked Excel P&L tab. The point of phase 1 is not generic diligence automation; it is to prove that deterministic workbook execution can outperform a sloppy AI-generated workbook on structure, traceability, and formula quality.

## Phase 1 Includes

- One narrow P&L pilot.
- Local execution only.
- Deterministic Python pipeline orchestration.
- LLM usage only for structured extraction from messy source documents.
- Explicit source manifests, evidence tracking, validation reporting, and workbook export.
- A minimal CLI and local API for demo purposes.

## Phase 1 Excludes

- Full SaaS platform work.
- CRM automation.
- PowerPoint automation.
- Broad diligence workflows outside the P&L pilot.
- Agentic Excel editing or letting the model place values directly into workbook cells.
- Expanding beyond one strong output tab unless the user explicitly approves it.

## How To Run The Project

### Install

```bash
cd /Users/futurewarren/Desktop/Angelic
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -e ".[dev]"
cp .env.example .env
```

### Run The CLI

```bash
angelic-pilot run --data-room /Users/futurewarren/Desktop/Angelic/samples/data_room
```

### Run The API

```bash
angelic-api
```

## How To Run Tests

```bash
pytest
```

## Coding Conventions

- Prefer Python 3.11+ and keep modules small and inspectable.
- Use `pydantic` models for contracts at module boundaries.
- Keep pipeline stages separated by responsibility: ingest, extract, normalize, map, validate, export.
- Reserve the LLM for extraction only; all normalization, mapping, formulas, and workbook writing must be deterministic code.
- Prefer explicit config and typed objects over implicit magic.
- Save run artifacts to disk so outputs can be inspected without a debugger.
- Add concise docstrings and TODO markers where logic is intentionally deferred.
- Avoid unnecessary framework or frontend complexity in phase 1.
- Prefer deterministic heuristics or typed adapters over clever but opaque parsing.

## What "Done" Means

A task is done for phase 1 when:

- It improves the narrow P&L pilot directly.
- It preserves deterministic workbook generation.
- It maintains or improves source traceability.
- It includes tests or validation coverage appropriate to the change.
- It does not widen scope beyond the pilot without approval.
- It keeps outputs inspectable in local run artifacts.

## Rules For Source Traceability

- Every important extracted value must carry a stable source reference.
- Source references must point back to a concrete document and locator, not a vague description.
- If a value cannot be traced, it must be flagged in validation rather than silently written into Excel.
- Evidence should be stored as structured metadata first; workbook comments or trace tabs are presentation layers on top of that metadata.
- The benchmark AI workbook is never a source of truth for factual values.
- When a parser cannot preserve detailed page, sheet, cell, or section metadata, log that limitation in validation or notes.

## Rules For Deterministic Excel Writing

- The model must never write workbook cells directly.
- Workbook population must happen through deterministic Python functions only.
- Derived rows must be formulas, not hard-coded values.
- Layout, row order, formatting, and formula behavior should be encoded in code or config, not inferred by the model at write time.
- Hidden or supporting sheets like `Trace` and `Validation` are allowed and encouraged when they improve auditability.

## Scope Control

Ask before widening scope beyond the P&L pilot. That includes adding new databook tabs, introducing platform features, building a user-facing frontend, or broadening into generic technical diligence workflows.
