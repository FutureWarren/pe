"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";

import { ArrowLeft, FolderClock, Loader2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
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
          : "无法加载历史记录。请确认本机已运行 angelic-api。",
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
        err instanceof Error ? err.message : "打开该项目失败，请稍后重试。",
      );
    } finally {
      setOpeningId(null);
    }
  };

  return (
    <div className="page-shell space-y-8">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div className="space-y-2">
          <div className="flex items-center gap-2 text-[11px] font-semibold uppercase tracking-[0.22em] text-muted-foreground">
            <FolderClock className="h-4 w-4" />
           历史记录
          </div>
          <h1 className="text-3xl font-semibold tracking-tight text-foreground sm:text-4xl">
            已生成的项目
          </h1>
          <p className="max-w-2xl text-sm leading-7 text-muted-foreground">
            列表来自本机 Python 引擎写入的 <span className="font-mono text-xs">outputs/</span>{" "}
            目录。每条记录对应一次完整流水线运行；点击「打开」可将该次结果载入当前工作区并进入处理流程。
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button variant="outline" size="sm" onClick={() => void loadRuns()} disabled={loading}>
            {loading ? <Loader2 className="h-4 w-4 animate-spin" /> : null}
            刷新列表
          </Button>
          <Button asChild size="sm">
            <Link href="/">
              <ArrowLeft className="h-4 w-4" />
              新建导入
            </Link>
          </Button>
        </div>
      </div>

      {error ? (
        <Card className="border-destructive/40 bg-destructive/5">
          <CardHeader>
            <CardTitle className="text-base text-destructive">加载出现问题</CardTitle>
            <CardDescription>{error}</CardDescription>
          </CardHeader>
        </Card>
      ) : null}

      <Card>
        <CardHeader>
          <CardTitle>运行列表</CardTitle>
          <CardDescription>
            按运行时间倒序，最多显示最近 500 次。较早的记录仍保留在磁盘上，可通过 CLI 或 API 调整 limit。
          </CardDescription>
        </CardHeader>
        <CardContent>
          {loading ? (
            <div className="flex items-center gap-2 py-12 text-muted-foreground">
              <Loader2 className="h-5 w-5 animate-spin" />
              正在读取历史记录…
            </div>
          ) : runs.length === 0 ? (
            <p className="py-10 text-center text-sm text-muted-foreground">
              暂无运行记录。完成一次导入后，将在此出现。
            </p>
          ) : (
            <div className="overflow-x-auto rounded-lg border border-border">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>名称</TableHead>
                    <TableHead>运行 ID</TableHead>
                    <TableHead>时间</TableHead>
                    <TableHead>抽取后端</TableHead>
                    <TableHead>校验</TableHead>
                    <TableHead className="text-right">源文件数</TableHead>
                    <TableHead className="text-right">操作</TableHead>
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
                        <TableCell className="font-mono text-xs text-muted-foreground">
                          {run.run_id}
                        </TableCell>
                        <TableCell className="whitespace-nowrap text-sm text-muted-foreground">
                          {formatDateTime(run.created_at)}
                        </TableCell>
                        <TableCell className="capitalize">{run.extraction_backend}</TableCell>
                        <TableCell className="text-sm text-muted-foreground">
                          {summarizeValidation(run)}
                        </TableCell>
                        <TableCell className="text-right tabular-nums">
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
                              <Loader2 className="h-4 w-4 animate-spin" />
                            ) : (
                              "打开"
                            )}
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
