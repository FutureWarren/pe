"use client";

import { useRef, useState } from "react";
import Link from "next/link";

import { ArrowRight, Filter, ShieldAlert } from "lucide-react";

import { PageIntro } from "@/components/deals/page-intro";
import { StatusBadge } from "@/components/deals/status-badge";
import { WorkflowBanner } from "@/components/deals/workflow-banner";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Label } from "@/components/ui/label";
import { Select } from "@/components/ui/select";
import { useDealsStore } from "@/lib/deals-store";
import { Deal, ExceptionItem, Severity } from "@/lib/types";
import { getWorkflowSnapshot } from "@/lib/workflow";

interface ReviewQueueViewProps {
  deal: Deal;
}

export function ReviewQueueView({ deal }: ReviewQueueViewProps) {
  const { updateReviewItemStatus } = useDealsStore();
  const [severityFilter, setSeverityFilter] = useState<Severity | "All">("All");
  const [categoryFilter, setCategoryFilter] = useState("All");
  const [unresolvedOnly, setUnresolvedOnly] = useState(true);
  const [activityNote, setActivityNote] = useState(
    "Review items reflect the current mapping and extraction state of this deal.",
  );
  const queueRef = useRef<HTMLDivElement>(null);
  const items = deal.exceptions;
  const criticalOpenCount = items.filter(
    (item) => item.status === "Open" && item.severity === "Critical",
  ).length;

  const categories = Array.from(new Set(items.map((item) => item.category)));
  const filteredItems = items.filter((item) => {
    const matchesSeverity = severityFilter === "All" || item.severity === severityFilter;
    const matchesCategory = categoryFilter === "All" || item.category === categoryFilter;
    const matchesResolved = !unresolvedOnly || item.status === "Open";

    return matchesSeverity && matchesCategory && matchesResolved;
  });

  const updateItem = (exceptionId: string, status: ExceptionItem["status"], note: string) => {
    updateReviewItemStatus(deal.id, exceptionId, status);
    setActivityNote(note);
  };

  const workflow = getWorkflowSnapshot(deal);
  const reviewStage = workflow.stages.find((stage) => stage.key === "review")!;
  const resultsSummary =
    severityFilter === "All"
      ? unresolvedOnly
        ? `Showing ${filteredItems.length} unresolved review items.`
        : `Showing all ${filteredItems.length} review items.`
      : `Showing ${filteredItems.length} ${severityFilter.toLowerCase()} severity items${unresolvedOnly ? " that are still unresolved" : ""}.`;

  const focusQueue = (severity: Severity | "All", note: string) => {
    setSeverityFilter(severity);
    setCategoryFilter("All");
    setUnresolvedOnly(true);
    setActivityNote(note);
    window.requestAnimationFrame(() => {
      const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      queueRef.current?.scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth", block: "start" });
    });
  };

  const resolvePrimaryAction =
    workflow.blockingExceptions > 0 ? (
      <Button
        onClick={() => {
          // "All" + unresolved-only: a single-severity filter would hide other
          // open blockers (e.g. picking Critical hid the open High items).
          focusQueue("All", "Focused the queue on unresolved items.");
        }}
      >
        Focus Unresolved Items
        <ArrowRight className="h-4 w-4" />
      </Button>
    ) : workflow.openExceptions > 0 ? (
      <Button
        onClick={() => {
          focusQueue("All", "Focused the queue on the remaining unresolved exceptions.");
        }}
      >
        Clear Remaining Exceptions
        <ArrowRight className="h-4 w-4" />
      </Button>
    ) : (
      <Button asChild>
        <Link href={`/deals/${deal.id}/outputs`}>
          Generate Outputs
          <ArrowRight className="h-4 w-4" />
        </Link>
      </Button>
    );

  return (
    <div className="animate-fade-up space-y-6">
      <PageIntro
        eyebrow="Review queue"
        title="Resolve what the system should not silently decide for you."
        description="The review queue holds conflicting values, low-confidence mappings, unit problems, and support gaps so the deal team can work exceptions deliberately."
      />

      <WorkflowBanner
        step={reviewStage.step}
        label={reviewStage.label}
        status={reviewStage.status}
        message={
          workflow.blockingExceptions > 0
            ? `Resolve ${workflow.blockingExceptions} high-severity exceptions before generating final outputs.`
            : workflow.openExceptions > 0
              ? `${workflow.openExceptions} review items still need resolution before outputs can be finalized.`
              : "All review items are resolved. The workflow is ready to move into outputs."
        }
        helperText="Only approved rows and resolved exceptions will flow into the output package."
        metrics={[
          { label: "Blocking", value: `${workflow.blockingExceptions} high-severity open` },
          { label: "Open", value: `${workflow.openExceptions} unresolved items` },
          { label: "Next step", value: workflow.openExceptions > 0 ? "Finish review" : "Outputs" },
        ]}
        actions={resolvePrimaryAction}
      />

      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
        <Card>
          <CardHeader>
            <CardDescription>Exceptions in queue</CardDescription>
            <CardTitle as="p" className="text-3xl tabular-nums">{items.length}</CardTitle>
          </CardHeader>
        </Card>
        <Card>
          <CardHeader>
            <CardDescription>Critical / high</CardDescription>
            <CardTitle as="p" className="text-3xl tabular-nums">
              {items.filter((item) => item.severity === "Critical" || item.severity === "High").length}
            </CardTitle>
          </CardHeader>
        </Card>
        <Card>
          <CardHeader>
            <CardDescription>Open items</CardDescription>
            <CardTitle as="p" className="text-3xl tabular-nums">{items.filter((item) => item.status === "Open").length}</CardTitle>
          </CardHeader>
        </Card>
        <Card>
          <CardHeader>
            <CardDescription>Resolved or deferred</CardDescription>
            <CardTitle as="p" className="text-3xl tabular-nums">{items.filter((item) => item.status !== "Open").length}</CardTitle>
          </CardHeader>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Filter className="h-4 w-4 text-accent" />
            Filters
          </CardTitle>
          <CardDescription>Focus the review list by severity, category, and unresolved state.</CardDescription>
        </CardHeader>
        <CardContent className="grid gap-4 md:grid-cols-3">
          <div className="space-y-2">
            <Label htmlFor="severity-filter">Severity</Label>
            <Select
              id="severity-filter"
              value={severityFilter}
              onChange={(event) => setSeverityFilter(event.target.value as Severity | "All")}
            >
              <option value="All">All severities</option>
              <option value="Critical">Critical</option>
              <option value="High">High</option>
              <option value="Medium">Medium</option>
              <option value="Low">Low</option>
            </Select>
          </div>
          <div className="space-y-2">
            <Label htmlFor="category-filter">Category</Label>
            <Select
              id="category-filter"
              value={categoryFilter}
              onChange={(event) => setCategoryFilter(event.target.value)}
            >
              <option value="All">All categories</option>
              {categories.map((category) => (
                <option key={category} value={category}>
                  {category}
                </option>
              ))}
            </Select>
          </div>
          <div className="flex items-end">
            <label className="flex items-center gap-3 rounded-2xl border border-border bg-white/80 px-4 py-3 text-sm">
              <Checkbox checked={unresolvedOnly} onChange={() => setUnresolvedOnly((value) => !value)} />
              Unresolved only
            </label>
          </div>
        </CardContent>
      </Card>

      <div ref={queueRef} className="scroll-mt-24 space-y-3">
        <div
          key={activityNote}
          role="status"
          aria-live="polite"
          className="animate-note-in rounded-2xl border border-border bg-surface-muted px-4 py-3 text-sm text-muted-foreground"
        >
          {activityNote}
        </div>
        <div className="rounded-2xl border border-border bg-white/80 px-4 py-3">
          <div className="text-xs font-semibold uppercase tracking-[0.16em] text-muted-foreground">
            Queue focus
          </div>
          <div className="mt-2 text-sm font-medium text-foreground">{resultsSummary}</div>
        </div>
      </div>

      <div className="space-y-4">
        {filteredItems.map((item) => (
          <Card key={item.id}>
            <CardHeader>
              <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
                <div className="space-y-2">
                  <div className="flex flex-wrap items-center gap-2">
                    <StatusBadge value={item.severity} />
                    <StatusBadge value={item.category} />
                    <StatusBadge value={item.status} />
                  </div>
                  <CardTitle className="text-xl">{item.affectedLineItem}</CardTitle>
                  <CardDescription>{item.detail}</CardDescription>
                </div>
                <div className="rounded-2xl border border-border bg-surface-muted px-4 py-3 text-sm">
                  <div className="text-xs uppercase tracking-[0.16em] text-muted-foreground">
                    Assigned owner
                  </div>
                  <div className="mt-2 font-semibold">{item.assignedOwner}</div>
                </div>
              </div>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="rounded-2xl border border-border bg-white/80 p-4 text-sm leading-7 text-muted-foreground">
                Suggested resolution: <span className="font-semibold text-foreground">{item.suggestedResolution}</span>
              </div>
              <div className="flex flex-wrap gap-3">
                <Button
                  size="sm"
                  onClick={() =>
                    updateItem(item.id, "Approved", `Approved exception resolution for ${item.affectedLineItem}.`)
                  }
                >
                  Approve
                </Button>
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() =>
                    updateItem(item.id, "Deferred", `Deferred ${item.affectedLineItem} pending more support.`)
                  }
                >
                  Defer
                </Button>
              </div>
            </CardContent>
          </Card>
        ))}
      </div>

      {filteredItems.length === 0 ? (
        <Card>
          <CardContent className="mt-0 flex items-start gap-3 py-8 text-sm text-muted-foreground">
            <ShieldAlert className="mt-0.5 h-5 w-5 text-accent" />
            No exceptions match the current filters. Try widening the queue or turning off
            unresolved-only mode.
          </CardContent>
        </Card>
      ) : null}
    </div>
  );
}
