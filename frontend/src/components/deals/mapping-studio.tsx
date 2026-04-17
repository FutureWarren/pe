"use client";

import { useState } from "react";
import Link from "next/link";

import { ArrowRight, Combine, Link2, ShieldCheck, Sparkles, Split } from "lucide-react";

import { PageIntro } from "@/components/deals/page-intro";
import { StatusBadge } from "@/components/deals/status-badge";
import { WorkflowBanner } from "@/components/deals/workflow-banner";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Progress } from "@/components/ui/progress";
import { Select } from "@/components/ui/select";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { useDealsStore } from "@/lib/deals-store";
import { mappingTagOptions, unmappedCategory } from "@/lib/local-pipeline";
import { getFileName } from "@/lib/mock-data";
import { Deal, MappingRow } from "@/lib/types";
import { getWorkflowSnapshot } from "@/lib/workflow";

interface MappingStudioViewProps {
  deal: Deal;
}

export function MappingStudioView({ deal }: MappingStudioViewProps) {
  const { updateMappingRow } = useDealsStore();
  const [activeRowId, setActiveRowId] = useState(deal.mappingRows[0]?.id ?? "");
  const [activityNote, setActivityNote] = useState(
    "Mapping changes now write back into the local workflow store, so review and outputs update from the same row state.",
  );
  const rows = deal.mappingRows;
  const selectedRowId = rows.some((row) => row.id === activeRowId) ? activeRowId : rows[0]?.id ?? "";
  const activeRow = rows.find((row) => row.id === selectedRowId) ?? rows[0];
  const approvedCount = rows.filter(
    (row) => row.status === "Approved" || row.status === "Rule Applied",
  ).length;
  const flaggedCount = rows.filter((row) => row.status === "Needs Review").length;
  const pendingCount = rows.filter((row) => row.status === "Pending").length;
  const workflow = getWorkflowSnapshot(deal);
  const mappingStage = workflow.stages.find((stage) => stage.key === "mapping")!;

  const updateRow = (rowId: string, updater: (row: MappingRow) => MappingRow, note: string) => {
    updateMappingRow(deal.id, rowId, updater);
    setActivityNote(note);
  };

  return (
    <div className="space-y-6">
      <PageIntro
        eyebrow="Mapping studio"
        title="Standardize raw line items without losing source traceability."
        description="This is the core workspace. Raw source labels stay visible, mapped databook tags stay editable, and the rationale stays attached to each row so review is explicit."
      />

      <WorkflowBanner
        step={mappingStage.step}
        label={mappingStage.label}
        status={mappingStage.status}
        message={
          workflow.unresolvedMappings > 0
            ? `${workflow.unresolvedMappings} rows are still unresolved before the databook can move forward.`
            : "All mapping rows are resolved and ready for review sign-off."
        }
        helperText="Mapped rows with unresolved issues will be routed into Review before outputs are finalized."
        metrics={[
          { label: "Approved", value: `${approvedCount} rows approved` },
          { label: "Flagged", value: `${flaggedCount} rows need review` },
          { label: "Pending", value: `${pendingCount} rows still being classified` },
        ]}
        actions={
          <Button asChild>
            <Link href={`/deals/${deal.id}/review`}>
              Send Flagged Rows to Review
              <ArrowRight className="h-4 w-4" />
            </Link>
          </Button>
        }
      />

      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
        <Card>
          <CardHeader>
            <CardDescription>Rows staged</CardDescription>
            <CardTitle className="text-3xl">{rows.length}</CardTitle>
          </CardHeader>
        </Card>
        <Card>
          <CardHeader>
            <CardDescription>Approved / rule-applied</CardDescription>
            <CardTitle className="text-3xl">{approvedCount}</CardTitle>
          </CardHeader>
        </Card>
        <Card>
          <CardHeader>
            <CardDescription>Flagged for review</CardDescription>
            <CardTitle className="text-3xl">{flaggedCount}</CardTitle>
          </CardHeader>
        </Card>
        <Card>
          <CardHeader>
            <CardDescription>Source linked</CardDescription>
            <CardTitle className="text-3xl">
              {rows.filter((row) => row.sourceLinked).length}/{rows.length}
            </CardTitle>
          </CardHeader>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Action rail</CardTitle>
          <CardDescription>
            Keep review actions concrete and visible rather than hidden in automated output.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="flex flex-wrap gap-3">
            <Button
              size="sm"
              variant="secondary"
              disabled={!activeRow}
              onClick={() =>
                activeRow
                  ? updateRow(
                      activeRow.id,
                      (row) => ({
                        ...row,
                        status: "Needs Review",
                        reasoning: `${row.reasoning} Split-line review requested.`,
                      }),
                      `Split line item requested for ${activeRow.rawLineItemLabel}.`,
                    )
                  : null
              }
            >
              <Split className="h-4 w-4" />
              Split a line item
            </Button>
            <Button
              size="sm"
              variant="secondary"
              disabled={!activeRow}
              onClick={() =>
                activeRow
                  ? updateRow(
                      activeRow.id,
                      (row) => ({
                        ...row,
                        status: "Approved",
                        reasoning: `${row.reasoning} Duplicate merge confirmed in mock flow.`,
                      }),
                      `Duplicate rows merged around ${activeRow.rawLineItemLabel}.`,
                    )
                  : null
              }
            >
              <Combine className="h-4 w-4" />
              Merge duplicates
            </Button>
            <Button
              size="sm"
              disabled={!activeRow}
              onClick={() =>
                activeRow
                  ? updateRow(
                      activeRow.id,
                      (row) => ({ ...row, status: "Rule Applied" }),
                      `Mock rule saved for future rows using the ${activeRow.mappedCategory} mapping.`,
                    )
                  : null
              }
            >
              <Sparkles className="h-4 w-4" />
              Apply rule to future rows
            </Button>
          </div>
          <div className="rounded-2xl border border-border bg-surface-muted px-4 py-3 text-sm text-muted-foreground">
            Active row: <span className="font-semibold text-foreground">{activeRow?.rawLineItemLabel}</span>
            {" • "}
            {activityNote}
          </div>
        </CardContent>
      </Card>

      <div className="grid gap-4 lg:grid-cols-[2.2fr_1.3fr_1.7fr]">
        <Card className="border-border bg-white/70">
          <CardHeader>
            <CardDescription>Source line items</CardDescription>
            <CardTitle className="text-lg">Raw labels, values, and period context</CardTitle>
          </CardHeader>
        </Card>
        <Card className="border-border bg-white/70">
          <CardHeader>
            <CardDescription>Mapped standard tags</CardDescription>
            <CardTitle className="text-lg">Explicit databook classification</CardTitle>
          </CardHeader>
        </Card>
        <Card className="border-border bg-white/70">
          <CardHeader>
            <CardDescription>Traceability and reasoning</CardDescription>
            <CardTitle className="text-lg">Source link visibility and review status</CardTitle>
          </CardHeader>
        </Card>
      </div>

      <Card className="overflow-hidden">
        <CardContent className="table-scroll mt-0 overflow-x-auto p-0">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Source file</TableHead>
                <TableHead>Source tab / page</TableHead>
                <TableHead>Raw line item label</TableHead>
                <TableHead>Raw value</TableHead>
                <TableHead>Period</TableHead>
                <TableHead>Mapped category</TableHead>
                <TableHead>Confidence</TableHead>
                <TableHead>Source link</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>Reasoning</TableHead>
                <TableHead>Actions</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {rows.map((row) => (
                <TableRow
                  key={row.id}
                  data-state={row.id === selectedRowId ? "active" : undefined}
                  className="cursor-pointer"
                  onClick={() => setActiveRowId(row.id)}
                >
                  <TableCell className="min-w-52 font-medium">
                    {getFileName(deal, row.sourceFileId)}
                  </TableCell>
                  <TableCell className="min-w-40 text-muted-foreground">{row.sourceLocator}</TableCell>
                  <TableCell className="min-w-56">
                    <div className="space-y-1">
                      <p className="font-semibold text-foreground">{row.rawLineItemLabel}</p>
                      <p className="text-xs uppercase tracking-[0.14em] text-muted-foreground">
                        Selected for review
                      </p>
                    </div>
                  </TableCell>
                  <TableCell className="font-mono text-sm">{row.rawValue}</TableCell>
                  <TableCell>{row.period}</TableCell>
                  <TableCell className="min-w-44">
                    <Select
                      value={row.mappedCategory}
                      onChange={(event) =>
                        updateRow(
                          row.id,
                          (currentRow) => ({
                            ...currentRow,
                            mappedCategory: event.target.value,
                            status:
                              event.target.value === unmappedCategory ? "Needs Review" : "Pending",
                            reasoning:
                              event.target.value === unmappedCategory
                                ? "No standard tag selected yet. Route this row to review."
                                : `Mapped manually in the local workflow to ${event.target.value}.`,
                          }),
                          `Mapped ${row.rawLineItemLabel} to ${event.target.value}.`,
                        )
                      }
                    >
                      {mappingTagOptions.map((tag) => (
                        <option key={tag} value={tag}>
                          {tag}
                        </option>
                      ))}
                    </Select>
                  </TableCell>
                  <TableCell className="min-w-40">
                    <div className="space-y-2">
                      <div className="text-xs text-muted-foreground">{row.confidence}%</div>
                      <Progress value={row.confidence} />
                    </div>
                  </TableCell>
                  <TableCell>
                    {row.sourceLinked ? (
                      <div className="inline-flex items-center gap-2 text-sm font-medium text-success">
                        <Link2 className="h-4 w-4" />
                        Linked
                      </div>
                    ) : (
                      <StatusBadge value="Missing" />
                    )}
                  </TableCell>
                  <TableCell>
                    <StatusBadge value={row.status} />
                  </TableCell>
                  <TableCell className="min-w-64 text-sm leading-6 text-muted-foreground">
                    {row.reasoning}
                  </TableCell>
                  <TableCell>
                    <div className="flex flex-col gap-2">
                      <Button
                        type="button"
                        size="sm"
                        variant="secondary"
                        onClick={(event) => {
                          event.stopPropagation();
                          updateRow(
                            row.id,
                            (currentRow) => ({ ...currentRow, status: "Approved" }),
                            `Approved ${row.rawLineItemLabel}.`,
                          );
                        }}
                      >
                        <ShieldCheck className="h-4 w-4" />
                        Approve
                      </Button>
                      <Button
                        type="button"
                        size="sm"
                        variant="outline"
                        onClick={(event) => {
                          event.stopPropagation();
                          updateRow(
                            row.id,
                            (currentRow) => ({ ...currentRow, status: "Needs Review" }),
                            `Flagged ${row.rawLineItemLabel} for additional review.`,
                          );
                        }}
                      >
                        Flag for review
                      </Button>
                    </div>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </CardContent>
      </Card>
    </div>
  );
}
