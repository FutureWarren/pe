# Architecture Notes

## Pipeline Stages

1. `app/ingest/`
   Discovers source files and produces stable document manifests plus parsed segments.
2. `app/extract/`
   Converts messy source content into schema-validated JSON facts.
3. `app/normalize/`
   Reconciles facts into canonical values using deterministic business rules.
4. `app/map/`
   Defines workbook row and cell bindings.
5. `app/validate/`
   Produces machine-readable validation output and human-readable run reports.
6. `app/export/`
   Writes deterministic Excel files using `openpyxl`.

## Non-Negotiable Rules

- LLM output must terminate at structured extraction artifacts.
- Final workbook cells are written only by deterministic Python code.
- Validation is required, not optional.
- Source traceability must remain visible and machine-readable.

## Current Status

This repo is currently at a runnable pilot level:

- source files are indexed into a deterministic registry
- parsed segments are emitted for supported file types
- P&L extraction emits JSON-only records
- resolved periods map into a deterministic workbook
- validation and export artifacts are written per run

Remaining TODOs are mostly about quality and breadth, not basic wiring.
