import { ReactNode } from "react";

import { ArrowRight, CheckCircle2, CircleAlert } from "lucide-react";

import { StatusBadge } from "@/components/deals/status-badge";
import { Card, CardContent } from "@/components/ui/card";

interface WorkflowBannerProps {
  step: number;
  label: string;
  status: string;
  message: string;
  helperText?: string;
  metrics: Array<{ label: string; value: string }>;
  actions?: ReactNode;
}

export function WorkflowBanner({
  step,
  label,
  status,
  message,
  helperText,
  metrics,
  actions,
}: WorkflowBannerProps) {
  return (
    <Card className="border-border-strong bg-white/78">
      <CardContent className="mt-0 flex flex-col gap-5 py-5 lg:flex-row lg:items-center lg:justify-between">
        <div className="space-y-4">
          <div className="flex flex-wrap items-center gap-2">
            <div className="inline-flex items-center gap-2 rounded-full border border-border bg-surface-muted px-3 py-1 text-[11px] font-semibold uppercase tracking-[0.16em] text-muted-foreground">
              <CheckCircle2 className="h-3.5 w-3.5" />
              Stage {step} of 5
            </div>
            <div className="inline-flex items-center gap-2 rounded-full border border-border bg-surface-muted px-3 py-1 text-[11px] font-semibold uppercase tracking-[0.16em] text-foreground">
              {label}
            </div>
            <StatusBadge value={status} />
          </div>
          <div className="space-y-2">
            <p className="max-w-3xl text-base font-semibold leading-7 text-foreground">{message}</p>
            {helperText ? (
              <p className="max-w-3xl text-sm leading-6 text-muted-foreground">{helperText}</p>
            ) : null}
          </div>
          <div className="flex flex-wrap gap-3">
            {metrics.map((metric) => (
              <div
                key={metric.label}
                className="rounded-2xl border border-border bg-surface-muted px-4 py-3"
              >
                <div className="text-[11px] font-semibold uppercase tracking-[0.16em] text-muted-foreground">
                  {metric.label}
                </div>
                <div className="mt-2 flex items-center gap-2 text-sm font-semibold text-foreground">
                  <CircleAlert className="h-4 w-4 text-muted-foreground" />
                  {metric.value}
                </div>
              </div>
            ))}
          </div>
        </div>

        {actions ? (
          <div className="flex shrink-0 flex-wrap items-center gap-3">
            {actions}
            <div className="hidden items-center gap-2 text-xs uppercase tracking-[0.16em] text-muted-foreground xl:inline-flex">
              Next action
              <ArrowRight className="h-3.5 w-3.5" />
            </div>
          </div>
        ) : null}
      </CardContent>
    </Card>
  );
}
