import Link from "next/link";

import { ArrowRight, CheckCircle2, CircleAlert, DatabaseZap, Files } from "lucide-react";

import { PageIntro } from "@/components/deals/page-intro";
import { StatusBadge } from "@/components/deals/status-badge";
import { WorkflowBanner } from "@/components/deals/workflow-banner";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Progress } from "@/components/ui/progress";
import { Deal } from "@/lib/types";
import { getWorkflowSnapshot } from "@/lib/workflow";
import {
  formatCompactCurrency,
  formatDateTime,
  formatInteger,
  formatPercent,
} from "@/lib/utils";

interface OverviewViewProps {
  deal: Deal;
}

export function OverviewView({ deal }: OverviewViewProps) {
  const workflow = getWorkflowSnapshot(deal);
  const activeStage = workflow.stages.find((stage) => stage.key === workflow.currentActionStage)!;
  const extractionStage = workflow.stages.find((stage) => stage.key === "extraction")!;
  const approvedMappings = deal.mappingRows.filter(
    (row) => row.status === "Approved" || row.status === "Rule Applied",
  ).length;
  const resolvedExceptions = deal.exceptions.filter((item) => item.status !== "Open").length;
  const outputCompleteness = Math.round(
    deal.outputs.reduce((total, output) => total + output.completeness, 0) / deal.outputs.length,
  );
  const primaryOverviewAction =
    workflow.currentActionStage === "review"
      ? {
          label: "Go to Review Queue",
          href: `/deals/${deal.id}/review`,
        }
      : workflow.currentActionStage === "mapping"
        ? {
            label: "Continue to Mapping",
            href: `/deals/${deal.id}/mapping`,
          }
        : workflow.currentActionStage === "outputs"
          ? {
              label: "Open Output Center",
              href: `/deals/${deal.id}/outputs`,
            }
          : {
              label: "Review Extraction",
              href: `/deals/${deal.id}/extraction`,
            };

  return (
    <div className="space-y-6">
      <PageIntro
        eyebrow={deal.sector}
        title={deal.targetCompanyName}
        description={`${deal.stage} • ${deal.geography} • ${deal.seller}. The workspace keeps source coverage, mapping, review, and output readiness visible in one place.`}
      />

      <WorkflowBanner
        step={activeStage.step}
        label={activeStage.label}
        status={activeStage.status}
        message={
          extractionStage.status === "Complete"
            ? `Extraction is complete. ${workflow.openExceptions} exceptions remain before outputs can be finalized.`
            : `Extraction is ${deal.extractionProgress}% complete. Mapping and review will stay provisional until staging is finished.`
        }
        helperText="The workflow rail shows which stage is complete, which stage needs attention now, and what is still blocking a clean output package."
        metrics={[
          {
            label: "Done",
            value: extractionStage.status === "Complete" ? "Extraction complete" : "Intake complete",
          },
          { label: "Remaining", value: `${workflow.openExceptions} exceptions still open` },
          { label: "Next best action", value: activeStage.label },
        ]}
        actions={
          <Button asChild>
            <Link href={primaryOverviewAction.href}>
              {primaryOverviewAction.label}
              <ArrowRight className="h-4 w-4" />
            </Link>
          </Button>
        }
      />

      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
        <Card>
          <CardHeader>
            <CardDescription>Enterprise value</CardDescription>
            <CardTitle className="text-3xl">{formatCompactCurrency(deal.enterpriseValue)}</CardTitle>
          </CardHeader>
        </Card>
        <Card>
          <CardHeader>
            <CardDescription>TTM revenue</CardDescription>
            <CardTitle className="text-3xl">{formatCompactCurrency(deal.ttmRevenue)}</CardTitle>
          </CardHeader>
        </Card>
        <Card>
          <CardHeader>
            <CardDescription>TTM EBITDA</CardDescription>
            <CardTitle className="text-3xl">{formatCompactCurrency(deal.ttmEbitda)}</CardTitle>
          </CardHeader>
        </Card>
        <Card>
          <CardHeader>
            <CardDescription>Readiness score</CardDescription>
            <CardTitle className="text-3xl">{deal.readinessScore}</CardTitle>
          </CardHeader>
        </Card>
      </div>

      <div className="grid gap-6 lg:grid-cols-[1.25fr_0.9fr]">
        <Card>
          <CardHeader>
            <CardTitle>Workflow progress</CardTitle>
            <CardDescription>Track readiness before the team trusts the final output package.</CardDescription>
          </CardHeader>
          <CardContent className="space-y-6">
            <div className="space-y-2">
              <div className="flex items-center justify-between text-sm text-muted-foreground">
                <span>Overall workflow completion</span>
                <span>{formatPercent(deal.workflowProgress)}</span>
              </div>
              <Progress value={deal.workflowProgress} className="h-3" />
            </div>

            <div className="grid gap-4 md:grid-cols-2">
              <div className="rounded-2xl border border-border bg-white/80 p-4">
                <div className="flex items-center gap-2 text-sm font-semibold">
                  <Files className="h-4 w-4 text-accent" />
                  Source coverage
                </div>
                <p className="mt-3 text-2xl font-semibold">{deal.sourceFiles.length} files</p>
                <p className="mt-2 text-sm text-muted-foreground">
                  Every key file stays visible in the sidebar and staging views.
                </p>
              </div>
              <div className="rounded-2xl border border-border bg-white/80 p-4">
                <div className="flex items-center gap-2 text-sm font-semibold">
                  <DatabaseZap className="h-4 w-4 text-accent" />
                  Mapping coverage
                </div>
                <p className="mt-3 text-2xl font-semibold">
                  {approvedMappings}/{deal.mappingRows.length}
                </p>
                <p className="mt-2 text-sm text-muted-foreground">
                  Approved or rules-applied mappings already tied back to source locators.
                </p>
              </div>
              <div className="rounded-2xl border border-border bg-white/80 p-4">
                <div className="flex items-center gap-2 text-sm font-semibold">
                  <CircleAlert className="h-4 w-4 text-warning" />
                  Exception resolution
                </div>
                <p className="mt-3 text-2xl font-semibold">
                  {resolvedExceptions}/{deal.exceptions.length}
                </p>
                <p className="mt-2 text-sm text-muted-foreground">
                  Exceptions remain visible instead of getting buried inside generated output.
                </p>
              </div>
              <div className="rounded-2xl border border-border bg-white/80 p-4">
                <div className="flex items-center gap-2 text-sm font-semibold">
                  <CheckCircle2 className="h-4 w-4 text-success" />
                  Output completeness
                </div>
                <p className="mt-3 text-2xl font-semibold">{formatPercent(outputCompleteness)}</p>
                <p className="mt-2 text-sm text-muted-foreground">
                  Output cards show what is complete, what is provisional, and what still needs review.
                </p>
              </div>
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Workflow next step</CardTitle>
            <CardDescription>One recommended action, with supporting pages underneath.</CardDescription>
          </CardHeader>
          <CardContent className="grid gap-3">
            <Link
              href={primaryOverviewAction.href}
              className="rounded-2xl border border-border-strong bg-white p-4 shadow-[0_14px_28px_rgba(20,31,45,0.06)] transition hover:bg-white"
            >
              <div className="flex items-center justify-between gap-3">
                <p className="font-semibold">{primaryOverviewAction.label}</p>
                <ArrowRight className="h-4 w-4 text-accent" />
              </div>
              <p className="mt-2 text-sm leading-6 text-muted-foreground">
                Keep the deal moving through the current active stage rather than treating every page
                as equally urgent.
              </p>
            </Link>
            <div className="grid gap-2 sm:grid-cols-3">
              <Link href={`/deals/${deal.id}/extraction`} className="rounded-2xl border border-border bg-white/80 p-3 text-sm text-muted-foreground transition hover:bg-white hover:text-foreground">
                Extraction
              </Link>
              <Link href={`/deals/${deal.id}/mapping`} className="rounded-2xl border border-border bg-white/80 p-3 text-sm text-muted-foreground transition hover:bg-white hover:text-foreground">
                Mapping
              </Link>
              <Link href={`/deals/${deal.id}/outputs`} className="rounded-2xl border border-border bg-white/80 p-3 text-sm text-muted-foreground transition hover:bg-white hover:text-foreground">
                Outputs
              </Link>
            </div>
          </CardContent>
        </Card>
      </div>

      <div className="grid gap-6 lg:grid-cols-[1.25fr_0.9fr]">
        <Card>
          <CardHeader>
            <CardTitle>Recent activity</CardTitle>
            <CardDescription>Visible workflow history builds trust in the process.</CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            {deal.recentActivity.map((activity) => (
              <div key={activity.id} className="rounded-2xl border border-border bg-white/80 p-4">
                <div className="flex items-start justify-between gap-4">
                  <div>
                    <p className="font-semibold">{activity.title}</p>
                    <p className="mt-2 text-sm leading-6 text-muted-foreground">
                      {activity.description}
                    </p>
                  </div>
                  <div className="text-xs uppercase tracking-[0.14em] text-muted-foreground">
                    {formatDateTime(activity.timestamp)}
                  </div>
                </div>
              </div>
            ))}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Deal summary</CardTitle>
            <CardDescription>Key context at a glance for the deal team.</CardDescription>
          </CardHeader>
          <CardContent className="space-y-4 text-sm">
            <div className="flex items-center justify-between rounded-2xl border border-border bg-white/80 px-4 py-3">
              <span className="text-muted-foreground">Sponsor</span>
              <span className="font-semibold">{deal.sponsor}</span>
            </div>
            <div className="flex items-center justify-between rounded-2xl border border-border bg-white/80 px-4 py-3">
              <span className="text-muted-foreground">Seller</span>
              <span className="font-semibold">{deal.seller}</span>
            </div>
            <div className="flex items-center justify-between rounded-2xl border border-border bg-white/80 px-4 py-3">
              <span className="text-muted-foreground">Geography</span>
              <span className="font-semibold">{deal.geography}</span>
            </div>
            <div className="flex items-center justify-between rounded-2xl border border-border bg-white/80 px-4 py-3">
              <span className="text-muted-foreground">Source files</span>
              <span className="font-semibold">{formatInteger(deal.sourceFiles.length)}</span>
            </div>
            <div className="flex items-center justify-between rounded-2xl border border-border bg-white/80 px-4 py-3">
              <span className="text-muted-foreground">Current status</span>
              <StatusBadge value={deal.status} />
            </div>
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
