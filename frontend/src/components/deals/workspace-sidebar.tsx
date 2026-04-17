"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

import { FileText, FolderOpenDot } from "lucide-react";

import { StatusBadge } from "@/components/deals/status-badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { cn } from "@/lib/utils";
import { Deal } from "@/lib/types";
import {
  getCurrentPageStage,
  getSourceFileWorkflowStatus,
  getWorkflowSnapshot,
  isSourceFileRelevantToPage,
} from "@/lib/workflow";

interface WorkspaceSidebarProps {
  deal: Deal;
  focusedFileId?: string | null;
}

export function WorkspaceSidebar({ deal, focusedFileId }: WorkspaceSidebarProps) {
  const pathname = usePathname();
  const workflow = getWorkflowSnapshot(deal);
  const currentPageStage = getCurrentPageStage(pathname);
  const overviewActive = pathname === `/deals/${deal.id}`;

  return (
    <div className="space-y-4">
      <Card className="sticky top-24">
        <CardHeader>
          <CardDescription className="uppercase tracking-[0.18em]">
            Deal workspace
          </CardDescription>
          <CardTitle className="text-2xl">{deal.targetCompanyName}</CardTitle>
          <div className="flex flex-wrap gap-2">
            <StatusBadge value={deal.status} />
            <StatusBadge value={`${deal.exceptionCount} exceptions`} />
          </div>
        </CardHeader>
        <CardContent className="space-y-6">
          <div className="space-y-2">
            <p className="text-xs font-semibold uppercase tracking-[0.16em] text-muted-foreground">
              Overview
            </p>
            <Link
              href={`/deals/${deal.id}`}
              className={cn(
                "flex items-center justify-between rounded-xl px-3 py-2.5 text-sm transition",
                overviewActive
                  ? "bg-accent text-accent-foreground shadow-[0_12px_24px_rgba(31,57,80,0.16)]"
                  : "text-muted-foreground hover:bg-white/80 hover:text-foreground",
              )}
            >
              <span className="flex items-center gap-2">
                <FolderOpenDot className="h-4 w-4" />
                Overview
              </span>
              {!overviewActive ? <StatusBadge value={workflow.stages.find((stage) => stage.key === workflow.currentActionStage)?.status ?? "In Progress"} /> : null}
            </Link>
          </div>

          <div className="space-y-3">
            <p className="text-xs font-semibold uppercase tracking-[0.16em] text-muted-foreground">
              Deal stages
            </p>
            <div className="space-y-3">
              {workflow.stages.map((stage, index) => {
                const active = currentPageStage === stage.key;
                const emphasized = !currentPageStage && workflow.currentActionStage === stage.key;
                const content = (
                  <div
                    className={cn(
                      "relative flex gap-3 rounded-2xl border px-3 py-3 transition",
                      active
                        ? "border-accent bg-accent text-accent-foreground shadow-[0_12px_24px_rgba(31,57,80,0.16)]"
                        : emphasized
                          ? "border-border-strong bg-white/85"
                          : "border-border bg-surface-muted hover:bg-white/80",
                    )}
                  >
                    {index < workflow.stages.length - 1 ? (
                      <div
                        className={cn(
                          "absolute left-[18px] top-10 h-[calc(100%+12px)] w-px",
                          active ? "bg-accent-foreground/25" : "bg-border",
                        )}
                      />
                    ) : null}
                    <div
                      className={cn(
                        "relative flex h-7 w-7 shrink-0 items-center justify-center rounded-full border text-xs font-semibold",
                        active
                          ? "border-accent-foreground/30 bg-accent-foreground/10 text-accent-foreground"
                          : "border-border-strong bg-white text-foreground",
                      )}
                    >
                      {stage.step}
                    </div>
                    <div className="min-w-0 flex-1 space-y-2">
                      <div className="flex items-start justify-between gap-3">
                        <div className="min-w-0">
                          <p className="text-sm font-semibold">{stage.label}</p>
                          <p
                            className={cn(
                              "mt-1 text-xs",
                              active ? "text-accent-foreground/70" : "text-muted-foreground",
                            )}
                          >
                            {stage.detail}
                          </p>
                        </div>
                        <StatusBadge value={stage.status} />
                      </div>
                    </div>
                  </div>
                );

                return stage.href ? (
                  <Link key={stage.key} href={stage.href}>
                    {content}
                  </Link>
                ) : (
                  <div key={stage.key}>{content}</div>
                );
              })}
            </div>
          </div>

          <div className="space-y-2">
            <p className="text-xs font-semibold uppercase tracking-[0.16em] text-muted-foreground">
              Source files
            </p>
            <div className="space-y-2">
              {deal.sourceFiles.map((file) => {
                const workflowStatus = getSourceFileWorkflowStatus(deal, file.id);
                const relevant = isSourceFileRelevantToPage(deal, file.id, pathname);
                const focused = focusedFileId === file.id;

                return (
                  <div
                    key={file.id}
                    className={cn(
                      "rounded-xl border px-3 py-3",
                      focused
                        ? "border-accent/25 bg-white shadow-[0_10px_24px_rgba(31,57,80,0.08)]"
                        : relevant
                        ? "border-border-strong bg-white/90"
                        : "border-border bg-surface-muted",
                    )}
                  >
                    <div className="flex items-start gap-3">
                      <FileText className="mt-0.5 h-4 w-4 text-muted-foreground" />
                      <div className="min-w-0 flex-1">
                        <p className="truncate text-sm font-medium text-foreground">{file.name}</p>
                        <p className="mt-1 text-xs text-muted-foreground">
                          {file.detectedCategory} • {file.fileType}
                        </p>
                        <div className="mt-2 flex items-center justify-between gap-2">
                          <span
                            className={cn(
                              "inline-flex rounded-full border px-2 py-1 text-[10px] font-semibold uppercase tracking-[0.16em]",
                              workflowStatus.tone === "success" &&
                                "border-success/20 bg-success/10 text-success",
                              workflowStatus.tone === "warning" &&
                                "border-warning/20 bg-warning/10 text-warning",
                              workflowStatus.tone === "danger" &&
                                "border-danger/20 bg-danger/10 text-danger",
                              workflowStatus.tone === "accent" &&
                                "border-accent/15 bg-accent/10 text-accent",
                              workflowStatus.tone === "muted" &&
                                "border-border bg-white/70 text-muted-foreground",
                            )}
                          >
                            {workflowStatus.label}
                          </span>
                          <div className="flex items-center gap-2">
                            {focused ? (
                              <span className="text-[10px] font-semibold uppercase tracking-[0.16em] text-accent">
                                Located
                              </span>
                            ) : null}
                            {relevant && !focused ? (
                              <span className="text-[10px] font-semibold uppercase tracking-[0.16em] text-muted-foreground">
                                In view
                              </span>
                            ) : null}
                          </div>
                        </div>
                      </div>
                    </div>
                  </div>
                );
              })}
            </div>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
