"use client";

import Link from "next/link";

import { ArrowRight, BriefcaseBusiness, CheckCircle2, FileSpreadsheet, ShieldCheck } from "lucide-react";

import { PageIntro } from "@/components/deals/page-intro";
import { StatusBadge } from "@/components/deals/status-badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Progress } from "@/components/ui/progress";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { useDealsStore } from "@/lib/deals-store";
import { formatCompactCurrency, formatPercent } from "@/lib/utils";

export function DealsDashboard() {
  const { deals } = useDealsStore();
  const averageReadiness =
    deals.length > 0
      ? Math.round(deals.reduce((total, deal) => total + deal.readinessScore, 0) / deals.length)
      : 0;
  const openExceptions = deals.reduce((total, deal) => total + deal.exceptionCount, 0);
  const outputsReady = deals.filter((deal) => deal.outputsReady).length;

  return (
    <div className="space-y-8">
      <PageIntro
        eyebrow="Workspace-first deal flow"
        title="Turn messy diligence files into a clean, reviewable databook workflow."
        description="This local prototype now carries a first real browser-side workflow for CSV and XLSX files, while keeping the interface centered on source files, mapping, traceability, and review rather than chat."
        actions={
          <>
            <Button asChild>
              <Link href="/deals/new">
                Create New Deal
                <ArrowRight className="h-4 w-4" />
              </Link>
            </Button>
            <Button asChild variant="secondary">
              <Link href="/deals/northstar-software">Open Sample Workspace</Link>
            </Button>
          </>
        }
      />

      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
        <Card>
          <CardHeader>
            <CardDescription>Active deals</CardDescription>
            <CardTitle className="flex items-center justify-between text-3xl">
              {deals.length}
              <BriefcaseBusiness className="h-5 w-5 text-muted-foreground" />
            </CardTitle>
          </CardHeader>
        </Card>
        <Card>
          <CardHeader>
            <CardDescription>Average readiness</CardDescription>
            <CardTitle className="flex items-center justify-between text-3xl">
              {formatPercent(averageReadiness)}
              <ShieldCheck className="h-5 w-5 text-muted-foreground" />
            </CardTitle>
          </CardHeader>
        </Card>
        <Card>
          <CardHeader>
            <CardDescription>Open exceptions</CardDescription>
            <CardTitle className="flex items-center justify-between text-3xl">
              {openExceptions}
              <FileSpreadsheet className="h-5 w-5 text-muted-foreground" />
            </CardTitle>
          </CardHeader>
        </Card>
        <Card>
          <CardHeader>
            <CardDescription>Outputs ready</CardDescription>
            <CardTitle className="flex items-center justify-between text-3xl">
              {outputsReady}
              <CheckCircle2 className="h-5 w-5 text-muted-foreground" />
            </CardTitle>
          </CardHeader>
        </Card>
      </div>

      <Card className="overflow-hidden">
        <CardHeader className="border-b border-border pb-4">
          <CardTitle>Deal pipeline</CardTitle>
          <CardDescription>
            A clean working queue for intake, extraction, mapping, review, and output readiness.
          </CardDescription>
        </CardHeader>
        <CardContent className="mt-0 hidden overflow-x-auto lg:block">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Target</TableHead>
                <TableHead>Sector</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>Source files</TableHead>
                <TableHead>Extraction</TableHead>
                <TableHead>Exceptions</TableHead>
                <TableHead>Outputs</TableHead>
                <TableHead className="text-right">EV / Revenue</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {deals.map((deal) => (
                <TableRow key={deal.id}>
                  <TableCell>
                    <div className="space-y-1">
                      <Link
                        className="font-semibold text-foreground transition hover:text-accent"
                        href={`/deals/${deal.id}`}
                      >
                        {deal.targetCompanyName}
                      </Link>
                      <p className="text-xs uppercase tracking-[0.16em] text-muted-foreground">
                        {deal.stage}
                      </p>
                    </div>
                  </TableCell>
                  <TableCell>{deal.sector}</TableCell>
                  <TableCell>
                    <StatusBadge value={deal.status} />
                  </TableCell>
                  <TableCell>{deal.sourceFilesConnected ? "Yes" : "No"}</TableCell>
                  <TableCell className="min-w-44">
                    <div className="space-y-2">
                      <div className="flex items-center justify-between text-xs text-muted-foreground">
                        <span>{deal.extractionProgress}% complete</span>
                        <span>{deal.readinessScore} score</span>
                      </div>
                      <Progress value={deal.extractionProgress} />
                    </div>
                  </TableCell>
                  <TableCell>{deal.exceptionCount}</TableCell>
                  <TableCell>{deal.outputsReady ? "Ready" : "Pending"}</TableCell>
                  <TableCell className="text-right">
                    <div className="space-y-1">
                      <div className="font-semibold">{formatCompactCurrency(deal.enterpriseValue)}</div>
                      <div className="text-xs text-muted-foreground">
                        {formatCompactCurrency(deal.ttmRevenue)} revenue
                      </div>
                    </div>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </CardContent>
        <CardContent className="mt-0 grid gap-4 lg:hidden">
          {deals.map((deal) => (
            <Link href={`/deals/${deal.id}`} key={deal.id}>
              <Card className="border-border bg-white/80 transition hover:-translate-y-0.5 hover:bg-white">
                <CardHeader>
                  <div className="flex items-start justify-between gap-4">
                    <div>
                      <CardTitle className="text-xl">{deal.targetCompanyName}</CardTitle>
                      <CardDescription>{deal.sector}</CardDescription>
                    </div>
                    <StatusBadge value={deal.status} />
                  </div>
                </CardHeader>
                <CardContent className="space-y-4">
                  <div className="grid grid-cols-2 gap-3 text-sm text-muted-foreground">
                    <div>Files connected: {deal.sourceFilesConnected ? "Yes" : "No"}</div>
                    <div>Exceptions: {deal.exceptionCount}</div>
                    <div>Outputs ready: {deal.outputsReady ? "Yes" : "No"}</div>
                    <div>Revenue: {formatCompactCurrency(deal.ttmRevenue)}</div>
                  </div>
                  <Progress value={deal.extractionProgress} />
                </CardContent>
              </Card>
            </Link>
          ))}
        </CardContent>
      </Card>
    </div>
  );
}
