import {
  AlertTriangle,
  ArrowRight,
  Eye,
  FileText,
  LayoutPanelLeft,
  ScanText,
} from "lucide-react";
import Link from "next/link";

import { PageIntro } from "@/components/deals/page-intro";
import { StatusBadge } from "@/components/deals/status-badge";
import { WorkflowBanner } from "@/components/deals/workflow-banner";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Progress } from "@/components/ui/progress";
import { getFileName } from "@/lib/mock-data";
import { Deal } from "@/lib/types";
import { getWorkflowSnapshot } from "@/lib/workflow";
import { formatPercent } from "@/lib/utils";

interface ExtractionViewProps {
  deal: Deal;
}

export function ExtractionView({ deal }: ExtractionViewProps) {
  const workflow = getWorkflowSnapshot(deal);
  const extractionStage = workflow.stages.find((stage) => stage.key === "extraction")!;
  return (
    <div className="space-y-6">
      <PageIntro
        eyebrow="Extraction staging"
        title="Review what the system found before it enters mapping."
        description="The extraction view surfaces staged tables, source file provenance, and quality warnings before anything gets normalized into a databook schema."
      />

      <WorkflowBanner
        step={extractionStage.step}
        label={extractionStage.label}
        status={extractionStage.status}
        message={
          workflow.extractionItemsWithIssues > 0
            ? `${workflow.extractionItemsWithIssues} extracted tables still need validation before mapping can be finalized.`
            : "Extraction is clean enough to advance into mapping."
        }
        helperText="Validated extraction feeds the mapping stage. Anything ambiguous here should stay visible before it becomes a standardized databook row."
        metrics={[
          { label: "Extracted", value: `${deal.extractedItems.length} staged tables` },
          { label: "Needs validation", value: `${workflow.extractionItemsWithIssues} issue-flagged tables` },
          { label: "Next step", value: "Mapping" },
        ]}
        actions={
          <Button asChild>
            <Link href={`/deals/${deal.id}/mapping`}>
              Validate and Continue to Mapping
              <ArrowRight className="h-4 w-4" />
            </Link>
          </Button>
        }
      />

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <LayoutPanelLeft className="h-4 w-4 text-accent" />
            Source files in scope
          </CardTitle>
          <CardDescription>
            These files are feeding the current extraction pass. They also remain pinned in the
            workspace sidebar for reference.
          </CardDescription>
        </CardHeader>
        <CardContent className="grid gap-3 md:grid-cols-2 2xl:grid-cols-4">
          {deal.sourceFiles.map((file) => (
            <div key={file.id} className="rounded-2xl border border-border bg-white/80 p-4">
              <div className="flex items-start gap-3">
                <FileText className="mt-0.5 h-4 w-4 text-muted-foreground" />
                <div className="min-w-0 space-y-2">
                  <div>
                    <p className="truncate text-sm font-semibold">{file.name}</p>
                    <p className="text-xs text-muted-foreground">
                      {file.detectedCategory} • {file.pages} pages
                    </p>
                  </div>
                  <StatusBadge value={file.status} />
                </div>
              </div>
            </div>
          ))}
        </CardContent>
      </Card>

      <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_300px]">
        <div className="space-y-4">
          <Card className="border-border bg-white/70">
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <ScanText className="h-4 w-4 text-accent" />
                Extracted tables and detected content
              </CardTitle>
              <CardDescription>
                Review staged outputs before they are normalized into the databook structure.
              </CardDescription>
            </CardHeader>
          </Card>

          <div className="space-y-4">
            {deal.extractedItems.map((item) => (
              <Card key={item.id}>
                <CardHeader>
                  <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
                    <div>
                      <CardTitle className="text-xl">{item.title}</CardTitle>
                      <CardDescription>
                        {getFileName(deal, item.sourceFileId)} • {item.period}
                      </CardDescription>
                    </div>
                    <StatusBadge value={`${item.confidence}% confidence`} />
                  </div>
                </CardHeader>
                <CardContent className="space-y-4">
                  <div className="grid gap-4 md:grid-cols-2 2xl:grid-cols-4">
                    <div className="rounded-2xl border border-border bg-surface-muted p-4">
                      <p className="text-xs uppercase tracking-[0.16em] text-muted-foreground">
                        Source file
                      </p>
                      <p className="mt-2 font-semibold">{getFileName(deal, item.sourceFileId)}</p>
                    </div>
                    <div className="rounded-2xl border border-border bg-surface-muted p-4">
                      <p className="text-xs uppercase tracking-[0.16em] text-muted-foreground">
                        Period
                      </p>
                      <p className="mt-2 font-semibold">{item.period}</p>
                    </div>
                    <div className="rounded-2xl border border-border bg-surface-muted p-4">
                      <p className="text-xs uppercase tracking-[0.16em] text-muted-foreground">
                        Table type
                      </p>
                      <p className="mt-2 font-semibold">{item.detectedTableType}</p>
                    </div>
                    <div className="rounded-2xl border border-border bg-surface-muted p-4">
                      <p className="text-xs uppercase tracking-[0.16em] text-muted-foreground">
                        Confidence
                      </p>
                      <p className="mt-2 font-semibold">{formatPercent(item.confidence)}</p>
                    </div>
                  </div>
                  <Progress value={item.confidence} />
                  <div className="rounded-2xl border border-border bg-white/80 p-4">
                    <p className="text-sm leading-7 text-muted-foreground">{item.summary}</p>
                  </div>
                  <div className="flex flex-wrap gap-2">
                    {item.issueFlags.map((flag) => (
                      <StatusBadge key={flag} value={flag} />
                    ))}
                  </div>
                </CardContent>
              </Card>
            ))}
          </div>
        </div>

        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <ScanText className="h-4 w-4 text-accent" />
              Extraction quality
            </CardTitle>
            <CardDescription>Readable, explicit quality signals before mapping.</CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="grid gap-3">
              <div className="rounded-2xl border border-border bg-white/80 px-4 py-3">
                <p className="text-xs uppercase tracking-[0.16em] text-muted-foreground">Missing headers</p>
                <p className="mt-2 text-2xl font-semibold">{deal.qualityPanel.missingHeaders}</p>
              </div>
              <div className="rounded-2xl border border-border bg-white/80 px-4 py-3">
                <p className="text-xs uppercase tracking-[0.16em] text-muted-foreground">Duplicate files</p>
                <p className="mt-2 text-2xl font-semibold">{deal.qualityPanel.duplicateFiles}</p>
              </div>
              <div className="rounded-2xl border border-border bg-white/80 px-4 py-3">
                <p className="text-xs uppercase tracking-[0.16em] text-muted-foreground">Unreadable pages</p>
                <p className="mt-2 text-2xl font-semibold">{deal.qualityPanel.unreadablePages}</p>
              </div>
              <div className="rounded-2xl border border-border bg-white/80 px-4 py-3">
                <p className="text-xs uppercase tracking-[0.16em] text-muted-foreground">Unit ambiguity</p>
                <p className="mt-2 text-2xl font-semibold">{deal.qualityPanel.unitAmbiguity}</p>
              </div>
            </div>

            <div className="rounded-2xl border border-border bg-surface-muted p-4">
              <p className="text-xs uppercase tracking-[0.16em] text-muted-foreground">
                Confidence summary
              </p>
              <div className="mt-4 space-y-3 text-sm">
                <div>
                  <div className="mb-2 flex justify-between text-muted-foreground">
                    <span>High confidence</span>
                    <span>{deal.qualityPanel.confidenceSummary.high}</span>
                  </div>
                  <Progress value={deal.qualityPanel.confidenceSummary.high * 20} />
                </div>
                <div>
                  <div className="mb-2 flex justify-between text-muted-foreground">
                    <span>Medium confidence</span>
                    <span>{deal.qualityPanel.confidenceSummary.medium}</span>
                  </div>
                  <Progress value={deal.qualityPanel.confidenceSummary.medium * 20} />
                </div>
                <div>
                  <div className="mb-2 flex justify-between text-muted-foreground">
                    <span>Low confidence</span>
                    <span>{deal.qualityPanel.confidenceSummary.low}</span>
                  </div>
                  <Progress value={deal.qualityPanel.confidenceSummary.low * 20} />
                </div>
              </div>
            </div>

            <div className="rounded-2xl border border-warning/20 bg-warning/10 p-4">
              <div className="flex items-start gap-3">
                <AlertTriangle className="mt-0.5 h-5 w-5 text-warning" />
                <div className="space-y-1">
                  <p className="font-semibold text-warning">Why this matters</p>
                  <p className="text-sm leading-6 text-muted-foreground">
                    Weak staging quality is surfaced here so the team can review it before the
                    system treats any extracted values as databook-ready.
                  </p>
                </div>
              </div>
            </div>

            <div className="rounded-2xl border border-border bg-white/80 p-4">
              <div className="flex items-center gap-2 text-sm font-semibold">
                <Eye className="h-4 w-4 text-accent" />
                Review posture
              </div>
              <p className="mt-2 text-sm leading-6 text-muted-foreground">
                Extraction is a staging view, not a final answer. Each table still needs mapping,
                confidence review, and visible traceability.
              </p>
            </div>
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
