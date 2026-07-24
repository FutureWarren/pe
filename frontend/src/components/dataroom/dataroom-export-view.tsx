"use client";

import { useState } from "react";
import Link from "next/link";

import { ArrowLeft, Download, FileSpreadsheet } from "lucide-react";

import { DataroomStepStrip } from "@/components/dataroom/dataroom-step-strip";
import { DealNotFoundState } from "@/components/deals/deal-not-found-state";
import { PageIntro } from "@/components/deals/page-intro";
import { StatusBadge } from "@/components/deals/status-badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Progress } from "@/components/ui/progress";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { useDealById, useDealsStore } from "@/lib/deals-store";
import { buildBackendRunSubtitle, isBackendDealReadyForExport } from "@/lib/backend-pipeline";
import { downloadDatabookCsv, downloadDatabookXlsx } from "@/lib/export";
import { getDatabookReadiness } from "@/lib/formula-engine";
import { getDatabookMetrics } from "@/lib/local-pipeline";
import { isBlockingCoreIssue, isNonBlockingRowIssue, isTableWarning } from "@/lib/review-utils";

interface DataroomExportViewProps {
  dealId: string;
}

export function DataroomExportView({ dealId }: DataroomExportViewProps) {
  const deal = useDealById(dealId);
  const { hydrated } = useDealsStore();
  const backendRunSubtitle = deal ? buildBackendRunSubtitle(deal) : null;
  const [activityNote, setActivityNote] = useState(
    "The export output is a workbook with source rows, understood rows, databook inputs, final metrics, traceability, and any open review items.",
  );

  if (!deal) {
    if (!hydrated) {
      return <div className="p-8 text-sm text-muted-foreground">Loading…</div>;
    }
    return <DealNotFoundState />;
  }

  const metrics = getDatabookMetrics(deal).filter((metric) => metric.status !== "Unavailable");
  const openExceptions = deal.exceptions.filter((item) => item.status === "Open");
  const blockingCount = openExceptions.filter((item) => isBlockingCoreIssue(item)).length;
  const nonBlockingCount = openExceptions.filter((item) => isNonBlockingRowIssue(item)).length;
  const tableWarningCount = openExceptions.filter((item) => isTableWarning(item)).length;
  const flaggedCount = blockingCount + nonBlockingCount;
  const reviewEntryCount = flaggedCount + tableWarningCount;
  const formulaBackedCount = metrics.filter(
    (metric) => metric.directOrDerived === "Derived" && metric.status === "Calculated",
  ).length;
  const directSourceBackedCount = metrics.filter(
    (metric) => metric.directOrDerived === "Direct",
  ).length;
  const tracedMetricCount = metrics.filter(
    (metric) => metric.traceabilityStatus === "Traced",
  ).length;
  const isBackendDeal = deal.processingEngine === "backend_python";
  const databookReadiness = getDatabookReadiness(metrics);
  const ready = isBackendDeal
    ? isBackendDealReadyForExport(deal)
    : databookReadiness.ready &&
      blockingCount === 0 &&
      metrics.length > 0;

  return (
    <div className="page-shell space-y-6">
      <section className="hero-panel animate-fade-up px-6 py-6 sm:px-7">
        <div className="space-y-6">
          <PageIntro
            eyebrow="Export"
            title="Export a clean databook."
            description={
              backendRunSubtitle
                ? `This is the final workbook from the Python pipeline. It tells you clearly whether export is ready, what still blocks it, and what open issues do not block it. ${backendRunSubtitle}`
                : "This is the final workbook. It tells you clearly whether export is ready, what still blocks it, and what open issues do not block it."
            }
            actions={
              <div className="flex flex-wrap gap-3">
                <Button asChild variant="secondary">
                  <Link href={`/process/${deal.id}`}>
                    <ArrowLeft className="h-4 w-4" />
                    Back to Processing
                  </Link>
                </Button>
                <Button
                  type="button"
                  onClick={async () => {
                    try {
                      await downloadDatabookXlsx(deal);
                      setActivityNote(
                        deal.processingEngine === "backend_python"
                          ? "Downloaded the Python-generated databook workbook (.xlsx)."
                          : "Downloaded the current databook workbook (.xlsx).",
                      );
                    } catch (err) {
                      setActivityNote(
                        err instanceof Error
                          ? `Download failed: ${err.message}`
                          : "Download failed. Please try again.",
                      );
                    }
                  }}
                  disabled={!ready}
                  className="shadow-raised"
                >
                  <Download className="h-4 w-4" />
                  Download XLSX
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
                Workbook posture
              </div>
              <div className="mt-2 text-2xl font-semibold">{ready ? "Ready to export" : "Blocked"}</div>
              <div className="mt-1 text-sm leading-6 text-muted-foreground">
                {ready
                  ? flaggedCount > 0
                    ? `${flaggedCount} open review item${flaggedCount === 1 ? "" : "s"} remain, but they do not block export.`
                    : tableWarningCount > 0
                      ? `${tableWarningCount} table warning${tableWarningCount === 1 ? "" : "s"} remain, but they do not block export.`
                    : "Direct values stay source-backed, while derived lines stay formula-backed."
                  : blockingCount > 0
                    ? `${blockingCount} blocking item${blockingCount === 1 ? "" : "s"} still need attention before export.`
                    : "The backend workbook is still finalizing its export state."}
              </div>
            </div>
            <div className="metric-panel px-4 py-4">
              <div className="text-[11px] font-semibold uppercase tracking-[0.18em] text-muted-foreground">
                Source-linked metrics
              </div>
              <div className="mt-2 text-2xl font-semibold">{tracedMetricCount}/{metrics.length}</div>
              <div className="mt-1 text-sm leading-6 text-muted-foreground">
                Exported metrics keep a source trail or formula trail.
              </div>
            </div>
            <div className="metric-panel px-4 py-4">
              <div className="text-[11px] font-semibold uppercase tracking-[0.18em] text-muted-foreground">
                Export file
              </div>
              <div className="mt-2 text-2xl font-semibold">XLSX workbook</div>
              <div className="mt-1 text-sm leading-6 text-muted-foreground">
                Multi-tab output designed to be inspected, reused, and shared.
              </div>
            </div>
          </div>

          <DataroomStepStrip currentStep="export" />
        </div>
      </section>

      <div className="grid gap-4 md:grid-cols-4">
        <Card className="lift-card animate-fade-up animate-delay-1 bg-white/[0.88]">
          <CardHeader className="space-y-4">
            <CardDescription>Databook status</CardDescription>
            <CardTitle className="text-xl">
              <StatusBadge value={ready ? "Ready" : "Blocked"} />
            </CardTitle>
            <Progress value={ready ? 100 : metrics.length > 0 ? 68 : 18} />
          </CardHeader>
        </Card>
        <Card className="lift-card animate-fade-up animate-delay-2 bg-white/[0.88]">
          <CardHeader className="space-y-4">
            <CardDescription>Calculated metrics</CardDescription>
            <CardTitle className="text-3xl">{formulaBackedCount}</CardTitle>
            <Progress
              value={metrics.length ? Math.round((formulaBackedCount / metrics.length) * 100) : 0}
            />
          </CardHeader>
        </Card>
        <Card className="lift-card animate-fade-up animate-delay-3 bg-white/[0.88]">
          <CardHeader className="space-y-4">
            <CardDescription>Source-backed metrics</CardDescription>
            <CardTitle className="text-3xl">{directSourceBackedCount}</CardTitle>
            <Progress
              value={metrics.length ? Math.round((directSourceBackedCount / metrics.length) * 100) : 0}
            />
          </CardHeader>
        </Card>
        <Card className="lift-card animate-fade-up animate-delay-4 bg-white/[0.88]">
          <CardHeader className="space-y-4">
            <CardDescription>Open review items</CardDescription>
            <CardTitle className="text-3xl">{flaggedCount}</CardTitle>
            <Progress value={ready ? 100 : Math.max(12, 100 - flaggedCount * 18)} />
          </CardHeader>
        </Card>
      </div>

      {reviewEntryCount > 0 ? (
        <Card className="lift-card animate-fade-up animate-delay-2 bg-white/[0.88]">
          <CardContent className="flex flex-col gap-3 py-6 md:flex-row md:items-center md:justify-between">
            <div className="space-y-1 text-sm">
              <div className="font-semibold text-foreground">
                {ready
                  ? `${flaggedCount} open review item${flaggedCount === 1 ? "" : "s"} remain, but export is still allowed`
                  : `${blockingCount} item${blockingCount === 1 ? "" : "s"} currently block export`}
              </div>
              <div className="text-muted-foreground">
                {ready
                  ? `These ${flaggedCount} item${flaggedCount === 1 ? "" : "s"} do not block the core databook export.`
                  : blockingCount > 0
                    ? `${nonBlockingCount} other item${nonBlockingCount === 1 ? "" : "s"} are open but non-blocking.${tableWarningCount > 0 ? ` ${tableWarningCount} table warning${tableWarningCount === 1 ? "" : "s"} are separate.` : ""}`
                    : "No blocking items are open. The backend workbook state just needs to finish syncing before export is unlocked."}
              </div>
            </div>
            <Button asChild variant="secondary">
              <Link href={`/process/${deal.id}/review`}>
                {tableWarningCount > 0 && flaggedCount === 0 ? "Open warnings" : "Open review items"}
                <FileSpreadsheet className="h-4 w-4" />
              </Link>
            </Button>
          </CardContent>
        </Card>
      ) : null}

      <div className="grid gap-4 md:grid-cols-3">
        <Card className="lift-card animate-fade-up animate-delay-2 bg-white/[0.88]">
          <CardHeader>
            <CardDescription>Workbook tabs</CardDescription>
            <CardTitle className="text-xl">5+ usable sheets</CardTitle>
          </CardHeader>
        </Card>
        <Card className="lift-card animate-fade-up animate-delay-3 bg-white/[0.88]">
          <CardHeader>
            <CardDescription>Traceable metrics</CardDescription>
            <CardTitle className="text-3xl">{tracedMetricCount}</CardTitle>
          </CardHeader>
        </Card>
        <Card className="lift-card animate-fade-up animate-delay-4 bg-white/[0.88]">
          <CardHeader>
            <CardDescription>Files used</CardDescription>
            <CardTitle className="text-3xl">{deal.sourceFiles.length}</CardTitle>
          </CardHeader>
        </Card>
      </div>

      <div className="surface-panel animate-fade-up animate-delay-3 px-4 py-3 text-sm text-muted-foreground">
        {activityNote}
      </div>

      <div className="grid gap-6 xl:grid-cols-[0.95fr_1.35fr]">
        <div className="hero-panel animate-fade-up animate-delay-4 rounded-2xl border border-border p-5 shadow-card backdrop-blur-sm">
          <div className="space-y-4">
            <div className="space-y-1">
              <CardTitle>Export result</CardTitle>
              <CardDescription>
                The workbook includes `Source_Raw`, `Defined_Items`, `Formula_Inputs`, `Databook`,
                and `Traceability`, plus `Review_Flags` when exceptions remain.
              </CardDescription>
            </div>

            <div className="surface-panel p-4 text-sm leading-7 text-muted-foreground">
              {ready
                ? flaggedCount > 0
                  ? "The workbook is ready to download. Some open review items remain, but they do not block the core databook export."
                  : tableWarningCount > 0
                    ? "The workbook is ready to download. Some table warnings remain, but they do not affect the core export gate."
                    : "The workbook is ready to download. Direct metrics stay source-backed, derived metrics stay formula-backed, and traceability is preserved in a separate tab."
                : blockingCount > 0
                  ? "The workbook is not ready yet. Clear the blocking core items and complete the missing P&L formulas first so the output remains low-error, traceable, and reusable."
                  : "The workbook content is ready, but the frontend is still waiting for the backend export state to settle."}
            </div>

            <div className="grid gap-3 sm:grid-cols-2">
              <div className="metric-panel px-4 py-4">
                <div className="text-[11px] uppercase tracking-[0.16em] text-muted-foreground">Primary output</div>
                <div className="mt-2 text-xl font-semibold">XLSX databook</div>
              </div>
              <div className="metric-panel px-4 py-4">
                <div className="text-[11px] uppercase tracking-[0.16em] text-muted-foreground">Fallback</div>
                <div className="mt-2 text-xl font-semibold">CSV extract</div>
              </div>
            </div>

            <div className="flex flex-wrap gap-3">
              {!ready ? (
                <Button asChild variant="secondary">
                  <Link href={`/process/${deal.id}/review`}>
                    {tableWarningCount > 0 && flaggedCount === 0 ? "Open warnings" : "Open review items"}
                    <FileSpreadsheet className="h-4 w-4" />
                  </Link>
                </Button>
              ) : null}
              <Button
                type="button"
                variant="outline"
                onClick={() => {
                  downloadDatabookCsv(deal);
                  setActivityNote("Downloaded the current databook CSV fallback.");
                }}
                disabled={!ready}
              >
                Download CSV
              </Button>
            </div>
          </div>
        </div>

        <Card className="lift-card animate-fade-up animate-delay-5 bg-white/[0.88]">
          <CardHeader>
            <CardTitle>Databook preview</CardTitle>
            <CardDescription>Final rows with value, source/formula context, and export status.</CardDescription>
          </CardHeader>
          <CardContent className="table-scroll mt-0 overflow-x-auto">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Metric</TableHead>
                  <TableHead>Value</TableHead>
                  <TableHead>Source / Calculated</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead>Source / formula</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {metrics.map((metric) => (
                  <TableRow key={metric.key}>
                    <TableCell className="font-medium">{metric.label}</TableCell>
                    <TableCell>{metric.formattedValue}</TableCell>
                    <TableCell>{metric.directOrDerived}</TableCell>
                    <TableCell>
                      <StatusBadge value={metric.status} />
                    </TableCell>
                    <TableCell className="text-sm text-muted-foreground">
                      {metric.sourceSummary}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
