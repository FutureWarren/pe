# Angelic Dataroom

Angelic Dataroom is a narrow local prototype for one job:

- import messy source files
- extract relevant financial data
- define what each line item actually is
- map it into a repeatable databook structure
- apply deterministic formulas
- export a clean databook workbook

This repo now contains two layers:

- `frontend/`: the main Next.js 16.2.2 Angelic Dataroom interface
- `app/`: the local Python P&L engine with Gemini-assisted extraction and deterministic workbook execution

The frontend remains the main product experience, but the workbook processing path now runs through the Python engine. The product stays intentionally narrow and centered on import -> process -> export rather than a broader PE workflow platform.

## Product Purpose

The v1 value proposition is:

- accurate
- low-error
- repeatable
- source-aware
- definition-backed
- formula-backed
- reusable on the next deal

This is meant to feel like a databook machine, not a large operating system.

## What The Current Prototype Includes

- Import screen with drag-and-drop local file upload
- Browser upload into the local Python processing engine
- Backend parsing for `.csv`, `.xlsx`, `.xls`, `.pdf`, `.docx`, and `.txt`
- Gemini-assisted extraction for messy document-style inputs where configured
- Deterministic normalization, validation, formula assignment, formula execution, and workbook writing in Python
- Definition layer that explains what each extracted item is, why it was categorized that way, and whether it is direct or derived
- Traceability layer that carries source file, sheet, row locator, and derivation path into the final output
- Deterministic formula layer for metrics like Gross Profit, Gross Margin, EBITDA, EBITDA Margin, ARR, Headcount, and CapEx
- Processing screen with import summary, definition coverage, traceability coverage, formula-backed metric coverage, and workbook readiness
- Secondary review-details screen for flagged items
- Export screen for workbook-first export with XLSX primary and CSV fallback
- Download of the Python-generated workbook through the frontend
- No auth, database, or cloud platform integrations

## Architecture Summary

The implementation is intentionally simple and easy to maintain.

- `frontend/src/app/`
  App Router pages and layouts
- `frontend/src/components/deals/`
  Product-specific workflow views like dashboard, mapping studio, review queue, and outputs
- `frontend/src/components/ui/`
  Small reusable UI primitives in a shadcn-style source-owned structure
- `frontend/src/lib/mock-data.ts`
  Seed deals and fallback demo content used to initialize the browser-local store
- `frontend/src/lib/deals-store.tsx`
  Lightweight client-side store backed by `localStorage`, now used mainly to persist backend run results for the UI
- `frontend/src/lib/backend-pipeline.ts`
  Frontend adapter that turns Python run payloads into UI-friendly deal state
- `frontend/src/lib/export.ts`
  Frontend download helpers that proxy the backend workbook and keep CSV fallback available
- `app/ingest/*`
  Deterministic manifest and parser layer for structured files and readable documents
- `app/extract/*`
  Extraction layer, with Gemini used only for structured extraction from messy sources
- `app/normalize/*`
  Deterministic normalization and reconciliation
- `app/map/*`
  Deterministic workbook row and cell binding rules
- `app/export/*`
  Deterministic Excel workbook writing
- `app/services/pipeline.py`
  The main Python orchestration path that produces inspectable run artifacts

Key product choices:

- Upload first
- Processing is the core experience
- Review is secondary and only for exceptions
- Definitions are explicit
- Traceability is explicit
- Deterministic formulas are explicit
- The frontend is the operator console; the Python engine is the source of truth for processing
- No heavy abstraction or unnecessary infrastructure

## Install

### Frontend MVP

```bash
cd /Users/futurewarren/Desktop/Angelic/frontend
npm install
cp .env.example .env.local
```

Set the local Python API base URL in `frontend/.env.local` if you are not using the default:

```bash
ANGELIC_API_BASE_URL=http://127.0.0.1:8011
```

The frontend no longer needs a Gemini key to process imports directly. Gemini is now used by the Python engine.

### Python Engine

```bash
cd /Users/futurewarren/Desktop/Angelic
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -e ".[dev]"
cp .env.example .env
```

To enable Gemini extraction in the Python engine, set these in `.env`:

```bash
ANGELIC_GEMINI_API_KEY=your_key_here
ANGELIC_GEMINI_MODEL=gemini-2.5-pro
```

## Run Locally

Run the Python engine first:

```bash
cd /Users/futurewarren/Desktop/Angelic
source .venv/bin/activate
angelic-api
```

Then run the frontend:

```bash
cd /Users/futurewarren/Desktop/Angelic/frontend
npm run dev
```

Open:

```text
http://localhost:3000
```

Useful frontend commands:

```bash
npm run lint
npm run build
```

Useful Python commands:

```bash
cd /Users/futurewarren/Desktop/Angelic
source .venv/bin/activate
pytest
angelic-pilot run --data-room /Users/futurewarren/Desktop/Angelic/samples/data_room
```

## Current Supported File Types

- Supported through the Python engine:
  - `.csv`
  - `.xlsx`
  - `.xls`
  - `.pdf`
  - `.docx`
  - `.txt`

- Strongest current starting point:
  - spreadsheet-style uploads like `.csv`, `.xlsx`, and `.xls`

- Still limited:
  - image-only PDFs without extractable text
  - unsupported binaries or file types outside the current parser set

## Current Processing Flow

1. Import files on the main screen
2. Send the upload set to the local Python pipeline
3. Build a manifest and parse supported files into structured segments
4. Use Gemini only for structured extraction from messy source content when configured
5. Normalize and reconcile extracted financial facts deterministically in Python
6. Assign deterministic workbook inputs and formulas in Python
7. Write the workbook deterministically in Python, including formula cells and trace tabs
8. Return structured run artifacts to the frontend for review and export
9. Surface only flagged issues in secondary review details
10. Download the generated workbook from the Python run artifacts

Browser persistence:

- imported run summaries, extracted items, defined items, review items, traceability records, and output state are stored in browser `localStorage`
- refreshing the page keeps the same processed import visible in the UI
- original file binaries are not stored in browser storage; backend run artifacts are the durable output

## What “Definition-Backed” Means

Each recognized source row is turned into an explicit `DefinedItem` before it becomes part of the final databook.

When Gemini is configured in the Python engine, it participates only in the extraction / interpretation step before deterministic formulas and workbook writing run.

A defined item captures:

- what the source line item is
- which databook category it maps into
- which workbook output line it should feed
- whether it is a direct input, a reported metric, or a review-only item
- which formula family or dependency set it belongs to
- why it was interpreted that way
- whether it is direct from source or treated as derived
- which source file, sheet, and locator it came from

This keeps interpretation out of hidden UI logic and makes the output inspectable.

## What “Traceable” Means

Every final databook metric should tie back to:

- a direct source-backed defined item
or
- a deterministic formula built from traced source-backed defined items

The workbook now carries this into a dedicated `Traceability` tab so the output does not contain mystery numbers.

## What “Formula-Backed Workbook” Means

The primary export is now an `.xlsx` workbook, not just a flat CSV.

The workbook includes:

- `Source_Raw`
- `Defined_Items`
- `Formula_Inputs`
- `Databook`
- `Traceability`
- `Review_Flags` when needed

The `Databook` tab separates:

- direct source-backed metrics
- derived formula-backed metrics

Core calculations like Gross Profit, Gross Margin, EBITDA, and EBITDA Margin are written as deterministic workbook formulas where the necessary inputs exist.

## Direct Vs Derived Metrics

Currently treated as direct, source-backed metrics:

- Revenue
- COGS
- Operating Expenses
- ARR
- Customer Churn
- Headcount
- CapEx
- Gross Profit or EBITDA when they are explicitly reported in source files and the base inputs are incomplete

Currently treated as derived, formula-backed metrics when the required inputs exist:

- Gross Profit = Revenue - COGS
- Gross Margin = Gross Profit / Revenue
- EBITDA = Gross Profit - Operating Expenses
- EBITDA Margin = EBITDA / Revenue

### Python Pilot

```bash
cd /Users/futurewarren/Desktop/Angelic
source .venv/bin/activate
angelic-pilot run --data-room /Users/futurewarren/Desktop/Angelic/samples/data_room
```

Run with Gemini-backed extraction:

```bash
cd /Users/futurewarren/Desktop/Angelic
source .venv/bin/activate
angelic-pilot run \
  --data-room /Users/futurewarren/Desktop/Angelic/samples/data_room \
  --extraction-backend gemini
```

Run the local API:

```bash
angelic-api
```

The local console at `http://127.0.0.1:8011/angelic-pilot` now also lets you choose
`deterministic` or `gemini` extraction before running the pilot.

## Push To GitHub

If this folder is not already a Git repo:

```bash
cd /Users/futurewarren/Desktop/Angelic
git init
git add .
git commit -m "Build Angelic frontend MVP"
git branch -M main
git remote add origin <YOUR_GITHUB_REPO_URL>
git push -u origin main
```

If the repo already exists and is already connected to GitHub:

```bash
cd /Users/futurewarren/Desktop/Angelic
git add frontend README.md
git commit -m "Build Angelic frontend MVP"
git push
```

## Deploy To Vercel

The frontend can still be deployed to Vercel, but the current product is a **local-first pilot** and now depends on the Python processing engine for real workbook generation.

If you deploy the frontend alone, it will render the UI, but imports will fail unless `ANGELIC_API_BASE_URL` points to a reachable Python API.

## Frontend Notes

- The app now uses the frontend as an operator console and the Python engine as the source of truth for processing.
- Uploaded run state persists in browser `localStorage` so the same processed import stays visible after refresh.
- The workbook export is now definition-backed, traceable, formula-backed, and downloaded from the Python run artifacts.
- Older multi-page workflow screens still exist in the codebase, but they are no longer the main product story.
- OCR, cloud integrations, and broader platform workflows remain out of scope.
- Advanced review details are intentionally secondary.

## What Remains Mocked

- OCR and scanned-document handling
- Cloud storage integrations
- CRM sync
- Multi-user collaboration
- Authentication
- Persistent backend job queue / async orchestration

## Existing Deterministic Pilot

The phase 1 Python pilot is now the main processing engine and still follows the narrow P&L principle:

- ingest source files
- Gemini-assisted or deterministic structured extraction
- deterministic normalization and mapping
- validation reporting
- workbook export

That code lives under `app/` and now powers the frontend import -> process -> export flow.

## Future Backend Roadmap

### 1. Real file ingestion

- Replace browser-local intake with storage-backed upload flows
- Create a deal record and file manifest in a backend service
- Trigger ingestion and extraction jobs after upload

### 2. Persistent mapping rules

- Store approved mappings and reusable rules in a database
- Let users apply prior decisions to future deals or future uploads
- Keep rule history auditable and easy to review

### 3. Source traceability storage

- Persist file metadata, source locators, extraction evidence, and review notes
- Separate evidence storage from presentation so trace data can power Excel comments, trace tabs, or audit views later
- Keep every important value tied to a stable source reference

### 4. CRM sync

- Add a backend integration layer that translates reviewed deal outputs into CRM-ready field updates
- Require explicit user approval before sync
- Log every outbound sync for auditability

### 5. True output generation

- Expand the current backend workbook generation into richer analyst-grade templates and validation packs
- Generate actual databook tabs, workbook files, validation reports, and review-note packages
- Keep all workbook writing deterministic and source-linked

## Limitations Of This Sprint

- Spreadsheet extraction is still heuristic for `.csv`, `.xlsx`, and `.xls`.
- Gemini extraction is now available for messy document-style inputs, but it is only used for structured JSON extraction.
- Table detection is practical rather than perfect; complex spreadsheets may not map cleanly.
- Workbook formulas currently focus on the core databook metrics, not every possible derived PE metric.
- The generated workbook is server-produced by the local Python engine, but the product still assumes one-user local operation rather than a shared backend deployment.
- Parsed run state persists locally in the browser, but original file contents are not re-opened from browser storage after refresh.
- Review actions in the frontend are local operator actions; they do not yet re-run the backend engine or persist collaborative approvals.

## Deterministic Today vs Future AI Plug-In

Deterministic today:

- spreadsheet row detection in the Python engine
- normalization and source-priority resolution
- formula calculation
- workbook writing

Gemini today:

- structured extraction from messy document-style inputs in the Python engine
- JSON output only
- no direct workbook writing
- no direct formula execution

Future AI provider plug-in point:

- `app/extract/gemini.py` is the current provider seam for document understanding and structured extraction
- even with a future provider change, normalization, formulas, workbook writing, and traceability should remain deterministic and inspectable

## What Success Looks Like

The MVP is doing its job if a user can open the app and understand the workflow without explanation:

- source files come in
- extraction is previewed before trust is granted
- mappings are reviewed row by row
- exceptions are visible
- outputs feel structured and reviewable

That is the core product story this repo is now optimized to demo.
