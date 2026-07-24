"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";

import { ArrowLeft, FolderClock, Loader2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { BackendPilotRunSummary, fetchPilotRunSummaries } from "@/lib/backend-pipeline";
import { useDealsStore } from "@/lib/deals-store";
import { formatDateTime } from "@/lib/utils";

function summarizeValidation(summary: BackendPilotRunSummary): string {
  if (summary.validation_status) {
    return `${summary.validation_status} · ${summary.issue_count ?? 0} issues`;
  }
  return "—";
}

export default function HistoryPage() {
  const router = useRouter();
  const { importDealFromBackendRunId } = useDealsStore();
  const [runs, setRuns] = useState<BackendPilotRunSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [openingId, setOpeningId] = useState<string | null>(null);

  const loadRuns = useCallback(async () => {
    setError(null);
    setLoading(true);
    try {
      const list = await fetchPilotRunSummaries(500);
      setRuns(list);
    } catch (err) {
      setError(
        err instanceof Error
          ? err.message
          : "Could not load history. Make sure the angelic-api backend is running.",
      );
      setRuns([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadRuns();
  }, [loadRuns]);

  const openRun = async (runId: string) => {
    setOpeningId(runId);
    setError(null);
    try {
      const deal = await importDealFromBackendRunId(runId);
      router.push(`/process/${deal.id}`);
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Could not open this run. Please try again.",
      );
    } finally {
      setOpeningId(null);
    }
  };

  return (
    <div className="page-shell space-y-8">
      <div className="animate-fade-up flex flex-wrap items-center justify-between gap-4">
        <div className="space-y-2">
          <div className="flex items-center gap-2 text-[11px] font-semibold uppercase tracking-[0.22em] text-muted-foreground">
            <FolderClock className="h-4 w-4" />
            History
          </div>
          <h1 className="text-3xl font-semibold tracking-tight text-foreground sm:text-4xl">
            Generated runs
          </h1>
          <p className="max-w-2xl text-sm leading-7 text-muted-foreground">
            Each entry is one full pipeline run written by the local Python engine to{" "}
            <span className="font-mono text-xs">outputs/</span>. Open a run to load its results
            into the current workspace and continue processing.
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button variant="outline" size="sm" onClick={() => void loadRuns()} disabled={loading}>
            {loading ? <Loader2 aria-hidden="true" className="h-4 w-4 animate-spin" /> : null}
            Refresh
          </Button>
          <Button asChild size="sm">
            <Link href="/">
              <ArrowLeft className="h-4 w-4" />
              New Import
            </Link>
          </Button>
        </div>
      </div>

      {error ? (
        <Card role="alert" className="animate-scale-in border-danger/30 bg-danger/5">
          <CardHeader>
            <CardTitle className="text-base text-danger">Something went wrong</CardTitle>
            <CardDescription>{error}</CardDescription>
          </CardHeader>
        </Card>
      ) : null}

      <Card className="animate-fade-up animate-delay-1">
        <CardHeader>
          <CardTitle>Run list</CardTitle>
          <CardDescription>
            Newest first, up to the most recent 500 runs. Older runs remain on disk and are
            available through the CLI or API.
          </CardDescription>
        </CardHeader>
        <CardContent>
          {loading ? (
            <div role="status" aria-label="Loading history" className="space-y-3 py-4">
              <Skeleton className="h-10 w-full" />
              <Skeleton className="h-10 w-full" />
              <Skeleton className="h-10 w-full" />
              <Skeleton className="h-10 w-full" />
              <Skeleton className="h-10 w-full" />
            </div>
          ) : runs.length === 0 ? (
            <p className="py-10 text-center text-sm text-muted-foreground">
              No runs yet. Finish an import and it will appear here.
            </p>
          ) : (
            <div className="table-scroll overflow-x-auto rounded-lg border border-border">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Name</TableHead>
                    <TableHead>Run ID</TableHead>
                    <TableHead>Created</TableHead>
                    <TableHead>Extraction</TableHead>
                    <TableHead>Validation</TableHead>
                    <TableHead className="text-right">Files</TableHead>
                    <TableHead className="text-right">Action</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {runs.map((run) => {
                    const label =
                      run.run_label?.trim() ||
                      run.input_paths?.data_room_dir?.split(/[/\\]/).filter(Boolean).pop() ||
                      run.run_id;
                    return (
                      <TableRow key={run.run_id}>
                        <TableCell className="max-w-[220px] font-medium">
                          <div className="truncate" title={label}>
                            {label}
                          </div>
                        </TableCell>
                        <TableCell className="max-w-[160px] font-mono text-xs text-muted-foreground">
                          <div className="truncate" title={run.run_id}>
                            {run.run_id}
                          </div>
                        </TableCell>
                        <TableCell className="whitespace-nowrap text-sm text-muted-foreground">
                          {formatDateTime(run.created_at)}
                        </TableCell>
                        <TableCell className="capitalize">{run.extraction_backend}</TableCell>
                        <TableCell className="text-sm text-muted-foreground">
                          {summarizeValidation(run)}
                        </TableCell>
                        <TableCell className="text-right font-mono text-sm tabular-nums">
                          {run.document_count ?? "—"}
                        </TableCell>
                        <TableCell className="text-right">
                          <Button
                            size="sm"
                            variant="secondary"
                            disabled={openingId !== null}
                            onClick={() => void openRun(run.run_id)}
                          >
                            {openingId === run.run_id ? (
                              <Loader2 aria-hidden="true" className="h-4 w-4 animate-spin" />
                            ) : null}
                            Open
                          </Button>
                        </TableCell>
                      </TableRow>
                    );
                  })}
                </TableBody>
              </Table>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
