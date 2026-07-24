"use client";

import { useState } from "react";
import Link from "next/link";

import { ArrowRight, Download, Eye, RefreshCcw } from "lucide-react";

import { PageIntro } from "@/components/deals/page-intro";
import { StatusBadge } from "@/components/deals/status-badge";
import { WorkflowBanner } from "@/components/deals/workflow-banner";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Progress } from "@/components/ui/progress";
import { useDealsStore } from "@/lib/deals-store";
import { downloadOutputCsv } from "@/lib/export";
import { Deal, OutputAsset } from "@/lib/types";
import { getWorkflowSnapshot } from "@/lib/workflow";
import { formatDate } from "@/lib/utils";

interface OutputCenterViewProps {
  deal: Deal;
}

export function OutputCenterView({ deal }: OutputCenterViewProps) {
  const { regenerateOutput: regenerateOutputInStore } = useDealsStore();
  const [activityNote, setActivityNote] = useState(
    "Exports are generated from the current mapping state of this deal.",
  );
  const outputs = deal.outputs;
  const isBackendDeal = deal.processingEngine === "backend_python";

  const regenerateOutput = (output: OutputAsset) => {
    regenerateOutputInStore(deal.id, output.id);
    // Use the clicked output directly — reading from the render-time `outputs`
    // closure reported on pre-update state. For backend deals the store only
    // refreshes metadata, so say that rather than claiming a regeneration.
    setActivityNote(
      isBackendDeal
        ? `Refreshed the status of ${output.name}. Re-run the import to regenerate backend outputs.`
        : `Regenerated ${output.name} from the current local extraction and mapping state.`,
    );
  };

  const exportOutput = (output: OutputAsset) => {
    downloadOutputCsv(deal, output);
    setActivityNote(`Exported ${output.name} as a real databook-style CSV file.`);
  };
  const workflow = getWorkflowSnapshot(deal);
  const outputsStage = workflow.stages.find((stage) => stage.key === "outputs")!;
  const previewTarget = outputs.find((output) => output.id === "databook-preview") ?? outputs[0];

  return (
    <div className="space-y-6">
      <PageIntro
        eyebrow="Output center"
        title="See what is ready to ship and what still needs review."
        description="Outputs are treated as visible deliverables with status, completeness, and source-link coverage instead of magical one-click artifacts."
      />

      <WorkflowBanner
        step={outputsStage.step}
        label={outputsStage.label}
        status={outputsStage.status}
        message={
          workflow.blockingExceptions > 0
            ? `Output package is blocked until ${workflow.blockingExceptions} high-severity review items are resolved.`
            : workflow.readyOutputs > 0
              ? "All critical steps are complete. Output package is ready to preview and export."
              : "Output package is still being assembled from the approved mapping set."
        }
        helperText="This package reflects the currently approved mapping set and resolved review items."
        metrics={[
          { label: "Ready outputs", value: `${workflow.readyOutputs} packages ready` },
          { label: "Review blockers", value: `${workflow.blockingExceptions} high-severity open` },
          { label: "Current state", value: outputsStage.detail },
        ]}
        actions={
          workflow.blockingExceptions > 0 ? (
            <Button asChild>
              <Link href={`/deals/${deal.id}/review`}>
                Return to Review Queue
                <ArrowRight className="h-4 w-4" />
              </Link>
            </Button>
          ) : previewTarget ? (
            <Button asChild>
              <Link href={`/deals/${deal.id}/outputs/${previewTarget.id}`}>
                Preview Databook
                <ArrowRight className="h-4 w-4" />
              </Link>
            </Button>
          ) : null
        }
      />

      <div className="rounded-2xl border border-border bg-surface-muted px-4 py-3 text-sm text-muted-foreground">
        {activityNote}
      </div>

      <div className="grid gap-4 xl:grid-cols-2">
        {outputs.map((output) => (
          <Card key={output.id}>
            <CardHeader>
              <div className="flex items-start justify-between gap-4">
                <div>
                  <CardTitle className="text-xl">{output.name}</CardTitle>
                  <CardDescription>Generated {formatDate(output.generatedDate)}</CardDescription>
                </div>
                <StatusBadge value={output.status} />
              </div>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="grid gap-3 md:grid-cols-2">
                <div className="rounded-2xl border border-border bg-white/80 px-4 py-3">
                  <p className="text-xs uppercase tracking-[0.16em] text-muted-foreground">Completeness</p>
                  <p className="mt-2 text-2xl font-semibold">{output.completeness}%</p>
                </div>
                <div className="rounded-2xl border border-border bg-white/80 px-4 py-3">
                  <p className="text-xs uppercase tracking-[0.16em] text-muted-foreground">Source linked</p>
                  <p className="mt-2 text-2xl font-semibold">{output.sourceLinked ? "Yes" : "No"}</p>
                </div>
                <div className="rounded-2xl border border-border bg-white/80 px-4 py-3 md:col-span-2">
                  <p className="text-xs uppercase tracking-[0.16em] text-muted-foreground">Review status</p>
                  <p className="mt-2 font-semibold">{output.reviewStatus}</p>
                </div>
              </div>

              <Progress value={output.completeness} />

              <div className="rounded-2xl border border-border bg-surface-muted p-4 text-sm leading-7 text-muted-foreground">
                {output.previewType === "table"
                  ? "Preview shows a source-linked table slice so the user understands the structure before export."
                  : "Preview shows a document-style notes package with sections and bullets grounded in the workspace state."}
              </div>

              <div className="flex flex-wrap gap-3">
                <Button asChild size="sm" variant="secondary">
                  <Link href={`/deals/${deal.id}/outputs/${output.id}`}>
                    <Eye className="h-4 w-4" />
                    Preview
                  </Link>
                </Button>
                <Button size="sm" variant="outline" onClick={() => exportOutput(output)}>
                  <Download className="h-4 w-4" />
                  Export
                </Button>
                <Button
                  size="sm"
                  onClick={() => regenerateOutput(output)}
                >
                  <RefreshCcw className="h-4 w-4" />
                  Regenerate
                </Button>
              </div>
            </CardContent>
          </Card>
        ))}
      </div>
    </div>
  );
}
