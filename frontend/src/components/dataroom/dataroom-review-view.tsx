"use client";

import Link from "next/link";

import { ArrowLeft, ArrowRight, ShieldAlert } from "lucide-react";

import { DealNotFoundState } from "@/components/deals/deal-not-found-state";
import { PageIntro } from "@/components/deals/page-intro";
import { StatusBadge } from "@/components/deals/status-badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { useDealById, useDealsStore } from "@/lib/deals-store";
import { getFileName } from "@/lib/mock-data";
import { isBlockingCoreIssue, isNonBlockingRowIssue, isTableWarning } from "@/lib/review-utils";

interface DataroomReviewViewProps {
  dealId: string;
}

export function DataroomReviewView({ dealId }: DataroomReviewViewProps) {
  const deal = useDealById(dealId);
  const { updateReviewItemStatus } = useDealsStore();

  if (!deal) {
    return <DealNotFoundState />;
  }

  const openItems = deal.exceptions.filter((item) => item.status === "Open");
  const blockingItems = openItems.filter((item) => isBlockingCoreIssue(item));
  const nonBlockingItems = openItems.filter((item) => isNonBlockingRowIssue(item));
  const tableWarnings = openItems.filter((item) => isTableWarning(item));
  const definedItems = deal.definedItems ?? [];
  const exportableRows = deal.mappingRows.filter(
    (row) =>
      row.entersCorePipeline !== false &&
      (row.status === "Approved" || row.status === "Rule Applied"),
  ).length;

  const groupedItems = [
    {
      title: "Blocking export",
      description: "These items need action before the core databook can be exported.",
      items: blockingItems,
      empty: "No items are currently blocking export.",
    },
    {
      title: "Open row issues",
      description: "These are real review items, but they do not stop the current core databook export.",
      items: nonBlockingItems,
      empty: "No non-blocking issues remain open.",
    },
    {
      title: "Table warnings",
      description: "These are extraction warnings tied to staged tables, not row-level databook blockers.",
      items: tableWarnings,
      empty: "No table warnings remain open.",
    },
  ];

  const issueLevelGroups = [
    { key: "metric", title: "Metric blockers", empty: "No metric-level issues here." },
    { key: "row", title: "Row issues", empty: "No row-level issues here." },
    { key: "table", title: "Table warnings", empty: "No table-level warnings here." },
  ] as const;

  return (
    <div className="page-shell space-y-6">
      <section className="hero-panel animate-fade-up px-6 py-6 sm:px-7">
          <PageIntro
            eyebrow="Open review items"
            title="Review the items that still need attention."
            description={`These items belong to the current import. ${blockingItems.length} ${blockingItems.length === 1 ? "item blocks" : "items block"} export right now, ${nonBlockingItems.length} ${nonBlockingItems.length === 1 ? "row issue is" : "row issues are"} non-blocking, and ${tableWarnings.length} ${tableWarnings.length === 1 ? "table warning is" : "table warnings are"} separate.`}
          actions={
            <div className="flex flex-wrap gap-3">
              <Button asChild variant="secondary">
                <Link href={`/process/${deal.id}`}>
                  <ArrowLeft className="h-4 w-4" />
                  Back to Processing
                </Link>
              </Button>
              <Button asChild className="shadow-[0_18px_38px_rgba(31,57,80,0.18)]">
                <Link href={`/process/${deal.id}/export`}>
                  Go to Export
                  <ArrowRight className="h-4 w-4" />
                </Link>
              </Button>
            </div>
          }
        />
      </section>

      <div className="grid gap-4 md:grid-cols-3">
        <Card className="lift-card animate-fade-up animate-delay-1 bg-white/[0.88]">
          <CardHeader>
            <CardDescription>Open review items</CardDescription>
            <CardTitle className="text-3xl">{blockingItems.length + nonBlockingItems.length}</CardTitle>
          </CardHeader>
        </Card>
        <Card className="lift-card animate-fade-up animate-delay-2 bg-white/[0.88]">
          <CardHeader>
            <CardDescription>Table warnings</CardDescription>
            <CardTitle className="text-3xl">{tableWarnings.length}</CardTitle>
          </CardHeader>
        </Card>
        <Card className="lift-card animate-fade-up animate-delay-3 bg-white/[0.88]">
          <CardHeader>
            <CardDescription>Rows ready in export</CardDescription>
            <CardTitle className="text-3xl">{exportableRows}</CardTitle>
          </CardHeader>
        </Card>
      </div>

      {openItems.length > 0 ? (
        <Card className="lift-card animate-fade-up animate-delay-4 bg-white/[0.88]">
          <CardContent className="flex flex-col gap-3 py-6 md:flex-row md:items-center md:justify-between">
            <div className="space-y-1 text-sm">
              <div className="font-semibold text-foreground">
                {blockingItems.length > 0
                  ? `${blockingItems.length} item${blockingItems.length === 1 ? "" : "s"} block export`
                  : "Export is not blocked by the remaining review items"}
              </div>
              <div className="text-muted-foreground">
                {blockingItems.length > 0
                  ? `${nonBlockingItems.length} other row issue${nonBlockingItems.length === 1 ? "" : "s"} are open but non-blocking.${tableWarnings.length > 0 ? ` ${tableWarnings.length} table warning${tableWarnings.length === 1 ? "" : "s"} are separate.` : ""}`
                  : `${nonBlockingItems.length} non-blocking row issue${nonBlockingItems.length === 1 ? "" : "s"} remain open.${tableWarnings.length > 0 ? ` ${tableWarnings.length} table warning${tableWarnings.length === 1 ? "" : "s"} are separate.` : ""}`}
              </div>
            </div>
            <div className="flex flex-wrap gap-3">
              <Button asChild variant="secondary">
                <Link href={`/process/${deal.id}`}>
                  Back to Processing
                </Link>
              </Button>
              <Button asChild>
                <Link href={blockingItems.length > 0 ? `/process/${deal.id}/review` : `/process/${deal.id}/export`}>
                  {blockingItems.length > 0 ? "Work through blocking items" : "Continue to Export"}
                  <ArrowRight className="h-4 w-4" />
                </Link>
              </Button>
            </div>
          </CardContent>
        </Card>
      ) : null}

      {openItems.length === 0 ? (
        <Card className="lift-card animate-fade-up animate-delay-5 bg-white/[0.88]">
          <CardContent className="mt-0 flex items-start gap-3 py-8 text-sm text-muted-foreground">
            <ShieldAlert className="mt-0.5 h-5 w-5 text-success" />
            No open review items remain. The databook is ready to export.
          </CardContent>
        </Card>
      ) : (
        <div className="space-y-6">
          {groupedItems.map((group) => (
            <div key={group.title} className="space-y-4">
              <div className="space-y-1">
                <h2 className="text-lg font-semibold text-foreground">{group.title}</h2>
                <p className="text-sm text-muted-foreground">{group.description}</p>
              </div>

              {group.items.length === 0 ? (
                <Card className="lift-card bg-white/[0.88]">
                  <CardContent className="py-6 text-sm text-muted-foreground">
                    {group.empty}
                  </CardContent>
                </Card>
              ) : (
                issueLevelGroups.map((issueLevelGroup) => {
            const levelItems = group.items.filter((item) => {
              if (group.title === "Table warnings") {
                return issueLevelGroup.key === "table";
              }

              return (item.issueLevel ?? "row") === issueLevelGroup.key;
            });

            if (levelItems.length === 0) {
              return null;
            }

            return (
              <div key={`${group.title}-${issueLevelGroup.key}`} className="space-y-4">
                <div className="text-sm font-semibold text-foreground">{issueLevelGroup.title}</div>
                {levelItems.map((item) => {
            const linkedRow = item.mappingRowId
              ? deal.mappingRows.find((row) => row.id === item.mappingRowId)
              : null;
            const linkedExtractedItem = item.extractedItemId
              ? deal.extractedItems.find((extractedItem) => extractedItem.id === item.extractedItemId)
              : null;
            const linkedDefinedItem = linkedRow
              ? definedItems.find((definedItem) => definedItem.id === `defined-${linkedRow.id}`)
              : null;

            return (
              <Card key={item.id} className="lift-card animate-fade-up animate-delay-5 bg-white/[0.88]">
                <CardHeader>
                  <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
                    <div className="space-y-2">
                      <div className="flex flex-wrap gap-2">
                        <StatusBadge value={item.blocksExport ? "Blocked" : "Non-blocking"} />
                        <StatusBadge value={item.severity} />
                        <StatusBadge value={item.category} />
                      </div>
                      <CardTitle className="text-xl">{item.affectedLineItem}</CardTitle>
                      <CardDescription>{item.detail}</CardDescription>
                    </div>
                    <div className="surface-panel px-4 py-3 text-sm">
                      <div className="text-xs uppercase tracking-[0.16em] text-muted-foreground">
                        Next step
                      </div>
                      <div className="mt-2 font-semibold">{item.suggestedResolution}</div>
                    </div>
                  </div>
                </CardHeader>
                <CardContent className="space-y-4">
                  <div className="grid gap-3 md:grid-cols-3">
                    <div className="metric-panel p-4">
                      <div className="text-xs uppercase tracking-[0.16em] text-muted-foreground">
                        Source file
                      </div>
                      <div className="mt-2 font-semibold">
                        {linkedRow
                          ? getFileName(deal, linkedRow.sourceFileId)
                          : linkedExtractedItem
                            ? getFileName(deal, linkedExtractedItem.sourceFileId)
                            : "Calculated metric"}
                      </div>
                    </div>
                    <div className="metric-panel p-4">
                      <div className="text-xs uppercase tracking-[0.16em] text-muted-foreground">
                        {item.issueLevel === "table" ? "Table warning" : "Definition / trace"}
                      </div>
                      <div className="mt-2 space-y-1">
                        <div className="font-semibold">
                          {item.issueLevel === "table"
                            ? linkedExtractedItem?.summary ?? linkedExtractedItem?.title ?? "Imported table"
                            : linkedDefinedItem?.definition ?? linkedRow?.sourceLocator ?? "Derived formula"}
                        </div>
                        <div className="text-sm text-muted-foreground">
                          {item.issueLevel === "table"
                            ? `${linkedExtractedItem?.tableName ?? linkedExtractedItem?.title ?? "Imported table"} · ${linkedExtractedItem?.issueFlags.join(", ") ?? "Warning"}`
                            : linkedDefinedItem
                            ? `${linkedDefinedItem.traceabilityStatus} · ${linkedDefinedItem.sourceLocation}`
                            : "Trace not available"}
                        </div>
                      </div>
                    </div>
                    <div className="metric-panel p-4">
                      <div className="text-xs uppercase tracking-[0.16em] text-muted-foreground">
                        {item.issueLevel === "metric" ? "Current metric" : item.issueLevel === "table" ? "Table type" : "Current category"}
                      </div>
                      <div className="mt-2 font-semibold">
                        {item.issueLevel === "metric"
                          ? item.affectedLineItem
                          : item.issueLevel === "table"
                            ? linkedExtractedItem?.detectedTableType ?? "Imported table"
                            : linkedRow?.mappedCategory ?? "Needs review"}
                      </div>
                      {linkedDefinedItem && item.issueLevel === "row" ? (
                        <div className="mt-2 text-sm text-muted-foreground">
                          {linkedDefinedItem.directOrDerived} · {linkedDefinedItem.calculationType}
                        </div>
                      ) : null}
                    </div>
                  </div>

                  <div className="flex flex-wrap gap-3">
                    <Button
                      type="button"
                      size="sm"
                      onClick={() => updateReviewItemStatus(deal.id, item.id, "Approved")}
                    >
                      Mark reviewed
                    </Button>
                    <Button
                      type="button"
                      size="sm"
                      variant="outline"
                      onClick={() => updateReviewItemStatus(deal.id, item.id, "Deferred")}
                    >
                      Keep open
                    </Button>
                  </div>
                </CardContent>
              </Card>
            );
                })}
              </div>
            );
                })
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
