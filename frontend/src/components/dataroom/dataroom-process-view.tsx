"use client";

import Link from "next/link";

import { ArrowRight, CheckCircle2, FileSpreadsheet, Sigma, TriangleAlert } from "lucide-react";

import { ModelInputWorkbench } from "@/components/dataroom/model-input-workbench";
import { DataroomStepStrip } from "@/components/dataroom/dataroom-step-strip";
import { DealNotFoundState } from "@/components/deals/deal-not-found-state";
import { PageIntro } from "@/components/deals/page-intro";
import { StatusBadge } from "@/components/deals/status-badge";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Progress } from "@/components/ui/progress";
import { useDealById } from "@/lib/deals-store";
import { buildBackendRunSubtitle, isBackendDealReadyForExport } from "@/lib/backend-pipeline";
import { getDatabookReadiness } from "@/lib/formula-engine";
import { buildDatabookExportRows, getDatabookMetrics, unmappedCategory } from "@/lib/local-pipeline";
import { isBlockingCoreIssue, isNonBlockingRowIssue, isTableWarning } from "@/lib/review-utils";

interface DataroomProcessViewProps {
  dealId: string;
}

export function DataroomProcessView({ dealId }: DataroomProcessViewProps) {
  const deal = useDealById(dealId);

  if (!deal) {
    // DealNotFoundState waits for store hydration itself (skeleton first).
    return <DealNotFoundState />;
  }

  const metrics = getDatabookMetrics(deal);
  const backendRunSubtitle = buildBackendRunSubtitle(deal);
  const fileTypes = Array.from(new Set(deal.sourceFiles.map((file) => file.fileType))).join(", ");
  const extractedRowCount = deal.extractedItems.reduce(
    (total, item) => total + (item.rows?.length ?? 0),
    0,
  );
  const definedItems = deal.definedItems ?? [];
  const coreDefinedItems = definedItems.filter((item) => item.entersCorePipeline !== false);
  const formulaInputs = deal.formulaInputs ?? [];
  const recognizedLineItems = deal.mappingRows.filter((row) => row.entersCorePipeline !== false).length;
  const openExceptions = deal.exceptions.filter((item) => item.status === "Open");
  const blockingCount = openExceptions.filter((item) => isBlockingCoreIssue(item)).length;
  const nonBlockingCount = openExceptions.filter((item) => isNonBlockingRowIssue(item)).length;
  const tableWarningCount = openExceptions.filter((item) => isTableWarning(item)).length;
  const flaggedCount = blockingCount + nonBlockingCount;
  const reviewEntryCount = flaggedCount + tableWarningCount;
  const mappedDefinedCount = coreDefinedItems.filter(
    (item) => item.mappedCategory !== unmappedCategory,
  ).length;
  const assignedFormulaInputs = formulaInputs.filter(
    (input) => input.assignedValue !== null,
  ).length;
  const tracedDefinedCount = coreDefinedItems.filter(
    (item) => item.traceabilityStatus === "Traced",
  ).length;
  const formulaMetrics = metrics.filter((metric) => metric.directOrDerived === "Derived");
  const formulaSuccessCount = formulaMetrics.filter((metric) => metric.status === "Calculated").length;
  const directMetricsCount = metrics.filter(
    (metric) => metric.directOrDerived === "Direct" && metric.status !== "Unavailable",
  ).length;
  const isBackendDeal = deal.processingEngine === "backend_python";
  const definitionCoverage = coreDefinedItems.length
    ? Math.round((mappedDefinedCount / coreDefinedItems.length) * 100)
    : 0;
  const traceabilityCoverage = coreDefinedItems.length
    ? Math.round((tracedDefinedCount / coreDefinedItems.length) * 100)
    : 0;
  const exportRows = buildDatabookExportRows(deal);
  const databookReadiness = getDatabookReadiness(metrics);
  const workbookReady = isBackendDeal
    ? isBackendDealReadyForExport(deal)
    : databookReadiness.ready &&
      blockingCount === 0 &&
      exportRows.length > 0;
  const primaryAction =
    !workbookReady
      ? {
          label: reviewEntryCount > 0 ? "Review flagged items" : "Back to import",
          href: reviewEntryCount > 0 ? `/process/${deal.id}/review` : "/",
        }
      : {
          label: "Export Databook",
          href: `/process/${deal.id}/export`,
        };

  const progressItems = [
    {
      label: "Files imported",
      status: deal.sourceFiles.length > 0 ? "Complete" : "Blocked",
      detail: `${deal.sourceFiles.length} files in the import set`,
      progress: deal.sourceFiles.length > 0 ? 100 : 0,
    },
    {
      label: "Rows found",
      status: extractedRowCount > 0 ? "Complete" : "Blocked",
      detail: `${extractedRowCount} rows were pulled from the imported files`,
      progress: extractedRowCount > 0 ? 100 : 0,
    },
    {
      label: "Rows understood",
      status:
        assignedFormulaInputs === 0
          ? "Blocked"
          : assignedFormulaInputs === formulaInputs.length
            ? "Complete"
            : "In Progress",
      detail: `${assignedFormulaInputs} of ${formulaInputs.length || coreDefinedItems.length || recognizedLineItems} rows are ready to feed the databook`,
      progress:
        formulaInputs.length || coreDefinedItems.length || recognizedLineItems
          ? Math.round(
              (assignedFormulaInputs /
                (formulaInputs.length || coreDefinedItems.length || recognizedLineItems)) *
                100,
            )
          : 0,
    },
    {
      label: "Source-linked rows",
      status:
        tracedDefinedCount === 0
          ? "Blocked"
          : tracedDefinedCount === coreDefinedItems.length
            ? "Complete"
            : "In Progress",
      detail: `${tracedDefinedCount} of ${coreDefinedItems.length} rows keep a usable source link`,
      progress: coreDefinedItems.length ? Math.round((tracedDefinedCount / coreDefinedItems.length) * 100) : 0,
    },
    {
      label: "Calculated metrics",
      status:
        formulaSuccessCount === 0
          ? "Blocked"
          : formulaSuccessCount === formulaMetrics.length
            ? "Complete"
            : "In Progress",
      detail: `${formulaSuccessCount} of ${formulaMetrics.length} calculated metrics are ready`,
      progress: formulaMetrics.length ? Math.round((formulaSuccessCount / formulaMetrics.length) * 100) : 0,
    },
    {
      label: "Ready to export",
      status: workbookReady ? "Ready" : "Blocked",
      detail:
        workbookReady
          ? `${exportRows.length} rows are ready in the export file`
          : `${blockingCount} blocking item${blockingCount === 1 ? "" : "s"} still need attention`,
      progress: workbookReady ? 100 : exportRows.length > 0 ? 72 : 18,
    },
  ] as const;

  return (
    <div className="page-shell space-y-6">
      <section className="hero-panel animate-fade-up px-6 py-6 sm:px-7">
        <div className="space-y-6">
          <PageIntro
            eyebrow="Process"
            badge={deal.isSample ? <Badge tone="warning">Sample data</Badge> : null}
            title="Process imported files into a clean databook structure."
            description={
              backendRunSubtitle
                ? `The Python pipeline parsed the files, Gemini helped interpret the extracted evidence, and deterministic logic calculated the databook metrics. ${backendRunSubtitle}`
                : "The system finds rows, understands which ones belong in the databook, calculates the core metrics, and tells you if export is ready or what still needs review."
            }
            actions={
              <div className="flex flex-wrap gap-3">
                <Button asChild className="shadow-raised">
                  <Link href={primaryAction.href}>
                    {primaryAction.label}
                    <ArrowRight className="h-4 w-4" />
                  </Link>
                </Button>
                {reviewEntryCount > 0 ? (
                  <Button asChild variant="secondary">
                    <Link href={`/process/${deal.id}/review`}>
                      {tableWarningCount > 0 && flaggedCount === 0
                        ? `Open warnings (${tableWarningCount})`
                        : `Open review items (${reviewEntryCount})`}
                      <FileSpreadsheet className="h-4 w-4" />
                    </Link>
                  </Button>
                ) : null}
              </div>
            }
          />

          <div className="grid gap-3 md:grid-cols-3">
            <div className="metric-panel px-4 py-4">
              <div className="text-[11px] font-semibold uppercase tracking-[0.18em] text-muted-foreground">
                Import footprint
              </div>
              <div className="mt-2 text-2xl font-semibold">{deal.sourceFiles.length} files</div>
              <div className="mt-1 text-sm leading-6 text-muted-foreground">
                {fileTypes || "No supported file types detected yet"} in this import.
              </div>
            </div>
            <div className="metric-panel px-4 py-4">
              <div className="text-[11px] font-semibold uppercase tracking-[0.18em] text-muted-foreground">
                Calculated metrics
              </div>
              <div className="mt-2 text-2xl font-semibold">
                {formulaSuccessCount}/{formulaMetrics.length || 0}
              </div>
              <div className="mt-1 text-sm leading-6 text-muted-foreground">
                Core formulas are running through the deterministic workbook engine.
              </div>
            </div>
            <div className="metric-panel px-4 py-4">
              <div className="text-[11px] font-semibold uppercase tracking-[0.18em] text-muted-foreground">
                Ready to export
              </div>
              <div className="mt-2 text-2xl font-semibold">{workbookReady ? "Ready" : "Needs review"}</div>
              <div className="mt-1 text-sm leading-6 text-muted-foreground">
                {workbookReady
                  ? flaggedCount > 0
                    ? `${flaggedCount} open review item${flaggedCount === 1 ? "" : "s"} remain, but they do not block the core export.`
                    : "The export path is clear."
                  : !isBackendDeal && databookReadiness.missingCoreMetricKeys.length > 0
                    ? `${databookReadiness.missingCoreMetricKeys.length} core P&L line${databookReadiness.missingCoreMetricKeys.length === 1 ? "" : "s"} still missing before export can be trusted.`
                    : blockingCount > 0
                      ? `${blockingCount} blocking item${blockingCount === 1 ? "" : "s"} still sit between processing and export.`
                      : "The backend workbook is still finalizing its export state."}
              </div>
            </div>
          </div>

          {reviewEntryCount > 0 ? (
            <div className="surface-panel flex flex-col gap-3 px-4 py-4 md:flex-row md:items-center md:justify-between">
              <div className="space-y-1">
                <div className="font-semibold text-foreground">
                  {flaggedCount} open review item{flaggedCount === 1 ? "" : "s"} for this import
                </div>
                <div className="text-sm text-muted-foreground">
                  {blockingCount > 0
                    ? `${blockingCount} block export right now. ${nonBlockingCount} are non-blocking.${tableWarningCount > 0 ? ` ${tableWarningCount} table warning${tableWarningCount === 1 ? "" : "s"} are separate.` : ""}`
                    : `None of these items block the core databook export. ${nonBlockingCount} are non-blocking.${tableWarningCount > 0 ? ` ${tableWarningCount} table warning${tableWarningCount === 1 ? "" : "s"} are separate.` : ""}`}
                </div>
              </div>
              <Button asChild variant="secondary">
                <Link href={`/process/${deal.id}/review`}>
                  {tableWarningCount > 0 && flaggedCount === 0 ? "Open warnings" : "Open review items"}
                  <ArrowRight className="h-4 w-4" />
                </Link>
              </Button>
            </div>
          ) : null}

          <DataroomStepStrip currentStep="process" />
        </div>
      </section>

      <div className="grid gap-4 xl:grid-cols-2">
        <Card className="lift-card animate-fade-up animate-delay-1 bg-white/[0.88]">
          <CardHeader>
            <CardTitle>Import summary</CardTitle>
            <CardDescription>What came in from the dataroom import.</CardDescription>
          </CardHeader>
          <CardContent className="grid gap-3 md:grid-cols-3">
            <div className="metric-panel px-4 py-4">
              <div className="text-xs uppercase tracking-[0.16em] text-muted-foreground">Files</div>
              <div className="mt-2 text-3xl font-semibold">{deal.sourceFiles.length}</div>
            </div>
            <div className="metric-panel px-4 py-4">
              <div className="text-xs uppercase tracking-[0.16em] text-muted-foreground">Types detected</div>
              <div className="mt-2 text-sm font-semibold">{fileTypes || "—"}</div>
            </div>
            <div className="metric-panel px-4 py-4">
              <div className="text-xs uppercase tracking-[0.16em] text-muted-foreground">Rows / tables extracted</div>
              <div className="mt-2 text-3xl font-semibold">
                {extractedRowCount} / {deal.extractedItems.length}
              </div>
            </div>
          </CardContent>
        </Card>

        <Card className="lift-card animate-fade-up animate-delay-2 bg-white/[0.88]">
          <CardHeader>
            <CardTitle>Processing summary</CardTitle>
            <CardDescription>What the system found, understood, and can already use in the databook.</CardDescription>
          </CardHeader>
          <CardContent className="grid gap-3 md:grid-cols-3">
            <div className="metric-panel px-4 py-4">
              <div className="text-xs uppercase tracking-[0.16em] text-muted-foreground">Rows found</div>
              <div className="mt-2 text-3xl font-semibold">{recognizedLineItems}</div>
            </div>
            <div className="metric-panel px-4 py-4">
              <div className="text-xs uppercase tracking-[0.16em] text-muted-foreground">Rows understood</div>
              <div className="mt-2 text-3xl font-semibold">{coreDefinedItems.length}</div>
            </div>
            <div className="metric-panel px-4 py-4">
              <div className="text-xs uppercase tracking-[0.16em] text-muted-foreground">Rows used in databook</div>
              <div className="mt-2 text-3xl font-semibold">
                {assignedFormulaInputs}/{formulaInputs.length || coreDefinedItems.length}
              </div>
            </div>
            <div className="metric-panel px-4 py-4">
              <div className="text-xs uppercase tracking-[0.16em] text-muted-foreground">Calculated metrics</div>
              <div className="mt-2 text-3xl font-semibold">
                {formulaSuccessCount}/{formulaMetrics.length}
              </div>
            </div>
            <div className="metric-panel px-4 py-4">
              <div className="text-xs uppercase tracking-[0.16em] text-muted-foreground">Source-linked rows</div>
              <div className="mt-2 text-3xl font-semibold">{traceabilityCoverage}%</div>
            </div>
              <div className="metric-panel px-4 py-4">
                <div className="text-xs uppercase tracking-[0.16em] text-muted-foreground">Needs review</div>
                <div className="mt-2 text-3xl font-semibold">{flaggedCount}</div>
              </div>
          </CardContent>
        </Card>
      </div>

      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <Card className="lift-card animate-fade-up animate-delay-2 bg-white/[0.88]">
          <CardHeader className="space-y-4">
            <CardDescription>Rows understood</CardDescription>
            <CardTitle as="p" className="text-3xl tabular-nums">{definitionCoverage}%</CardTitle>
            <Progress value={definitionCoverage} />
          </CardHeader>
        </Card>
        <Card className="lift-card animate-fade-up animate-delay-3 bg-white/[0.88]">
          <CardHeader className="space-y-4">
            <CardDescription>Source-linked rows</CardDescription>
            <CardTitle as="p" className="text-3xl tabular-nums">{traceabilityCoverage}%</CardTitle>
            <Progress value={traceabilityCoverage} />
          </CardHeader>
        </Card>
        <Card className="lift-card animate-fade-up animate-delay-4 bg-white/[0.88]">
          <CardHeader className="space-y-4">
            <CardDescription>Calculated metrics</CardDescription>
            <CardTitle as="p" className="text-3xl tabular-nums">{formulaSuccessCount}</CardTitle>
            <Progress
              value={formulaMetrics.length ? Math.round((formulaSuccessCount / formulaMetrics.length) * 100) : 0}
            />
          </CardHeader>
        </Card>
        <Card className="lift-card animate-fade-up animate-delay-5 bg-white/[0.88]">
          <CardHeader className="space-y-4">
            <CardDescription>Source-backed metrics</CardDescription>
            <CardTitle as="p" className="text-3xl tabular-nums">{directMetricsCount}</CardTitle>
            <Progress value={metrics.length ? Math.round((directMetricsCount / metrics.length) * 100) : 0} />
          </CardHeader>
        </Card>
      </div>

      <Card className="lift-card animate-fade-up animate-delay-3 bg-white/[0.88]">
        <CardHeader>
          <CardTitle>What is ready</CardTitle>
          <CardDescription>Simple status for this import from file upload to export.</CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          {progressItems.map((item) => (
            <div
              key={item.label}
              className="surface-panel flex flex-col gap-3 px-4 py-4 md:flex-row md:items-center md:justify-between"
            >
              <div className="space-y-2">
            <div className="font-semibold text-foreground">{item.label}</div>
            <div className="text-sm text-muted-foreground">{item.detail}</div>
            <Progress value={item.progress} className="max-w-xl" />
              </div>
              <StatusBadge value={item.status} />
            </div>
          ))}
        </CardContent>
      </Card>

      <ModelInputWorkbench deal={deal} />

      <Card className="lift-card animate-fade-up animate-delay-5 bg-white/[0.88]">
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Sigma className="h-4 w-4 text-accent" />
            Next action
          </CardTitle>
          <CardDescription>Keep the path from import to export minimal.</CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="surface-panel p-4">
            <div className="flex items-start gap-3">
              {blockingCount > 0 ? (
                <TriangleAlert className="mt-0.5 h-5 w-5 text-warning" />
              ) : (
                <CheckCircle2 className="mt-0.5 h-5 w-5 text-success" />
              )}
              <div className="space-y-1">
                <p className="font-semibold">
                  {!workbookReady
                    ? blockingCount > 0
                      ? "Review is still needed before export"
                      : "The workbook is still being finalized"
                    : flaggedCount > 0
                      ? "Export is ready, but some items are still open"
                      : tableWarningCount > 0
                        ? "Export is ready, with separate table warnings"
                        : "The databook is ready to export"}
                </p>
                <p className="text-sm leading-6 text-muted-foreground">
                  {!workbookReady
                    ? blockingCount > 0
                      ? `${blockingCount} item${blockingCount === 1 ? "" : "s"} block export right now. Open review items to resolve them.`
                      : "The backend run has finished, but the export status has not fully settled yet. Refresh or reopen this import once the workbook state updates."
                    : flaggedCount > 0
                      ? `${nonBlockingCount} non-blocking issue${nonBlockingCount === 1 ? "" : "s"} remain open, but the core databook is still ready to export.`
                      : tableWarningCount > 0
                        ? `${tableWarningCount} table warning${tableWarningCount === 1 ? "" : "s"} remain, but they do not enter the core export gate.`
                        : "The defined items, traceability links, and deterministic formulas are clean enough to produce a reusable workbook output."}
                </p>
              </div>
            </div>
          </div>

          <div className="grid gap-3 sm:grid-cols-3">
            <div className="metric-panel px-4 py-4">
              <div className="text-[11px] uppercase tracking-[0.16em] text-muted-foreground">Rows in export</div>
              <div className="mt-2 text-3xl font-semibold">{exportRows.length}</div>
            </div>
            <div className="metric-panel px-4 py-4">
              <div className="text-[11px] uppercase tracking-[0.16em] text-muted-foreground">Open review items</div>
              <div className="mt-2 text-3xl font-semibold">{flaggedCount}</div>
            </div>
            <div className="metric-panel px-4 py-4">
              <div className="text-[11px] uppercase tracking-[0.16em] text-muted-foreground">Table warnings</div>
              <div className="mt-2 text-3xl font-semibold">{tableWarningCount}</div>
            </div>
          </div>

          <div className="flex flex-wrap gap-3">
            <Button asChild>
              <Link href={primaryAction.href}>
                {primaryAction.label}
                <ArrowRight className="h-4 w-4" />
              </Link>
            </Button>

            <Button asChild variant="secondary">
              <Link href={`/process/${deal.id}/review`}>
                {tableWarningCount > 0 && flaggedCount === 0 ? "Open warnings" : "Open review items"}
                <FileSpreadsheet className="h-4 w-4" />
              </Link>
            </Button>
          </div>

          <div className="surface-panel space-y-3 p-4">
            <div className="flex items-center justify-between text-sm">
              <span className="text-muted-foreground">Databook readiness</span>
              <span className="font-semibold">{deal.readinessScore}%</span>
            </div>
            <Progress value={deal.readinessScore} />
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
