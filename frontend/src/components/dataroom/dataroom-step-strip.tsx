import { Check } from "lucide-react";

import { cn } from "@/lib/utils";

interface DataroomStepStripProps {
  currentStep: "import" | "process" | "export";
}

const steps = [
  {
    key: "import",
    label: "Import",
    detail: "Drop files in",
  },
  {
    key: "process",
    label: "Process",
    detail: "Extract and calculate",
  },
  {
    key: "export",
    label: "Export",
    detail: "Download databook",
  },
] as const;

export function DataroomStepStrip({ currentStep }: DataroomStepStripProps) {
  return (
    <div className="relative grid gap-3 sm:grid-cols-3">
      <div className="pointer-events-none absolute left-[11%] right-[11%] top-1/2 hidden h-px -translate-y-1/2 bg-[linear-gradient(90deg,rgba(19,32,45,0.02),rgba(19,32,45,0.14),rgba(19,32,45,0.02))] sm:block" />
      {steps.map((step, index) => {
        const active = step.key === currentStep;
        const completed =
          (currentStep === "process" || currentStep === "export") && step.key === "import"
            ? true
            : currentStep === "export" && step.key === "process";
        const statusLabel = active ? "Current" : completed ? "Complete" : "Next";

        return (
          <div
            key={step.key}
            className={cn(
              "relative rounded-2xl border px-4 py-4 backdrop-blur-sm animate-fade-up transition-colors duration-300",
              index === 0 ? "animate-delay-1" : index === 1 ? "animate-delay-2" : "animate-delay-3",
              active
                ? "border-accent bg-accent text-accent-foreground shadow-raised"
                : completed
                  ? "border-border-strong bg-white/[0.94] shadow-[0_14px_28px_rgba(19,32,45,0.06)]"
                  : "border-border bg-surface-muted shadow-[0_12px_24px_rgba(19,32,45,0.04)]",
            )}
          >
            {/* One status signal per step — the old extra "In focus/Passed/Queued"
                pill duplicated the label below and overflowed narrow cards. */}
            <div className="flex items-center gap-3">
              <div
                className={cn(
                  "flex h-9 w-9 shrink-0 items-center justify-center rounded-full border text-xs font-semibold shadow-[inset_0_1px_0_rgba(255,255,255,0.4)]",
                  active
                    ? "border-accent-foreground/[0.28] bg-accent-foreground/10 text-accent-foreground"
                    : completed
                      ? "border-border-strong bg-white text-foreground"
                      : "border-border-strong bg-white/[0.88] text-foreground",
                )}
              >
                {completed ? <Check className="h-4 w-4" aria-hidden="true" /> : index + 1}
              </div>
              <div className="min-w-0">
                <div className="text-sm font-semibold">{step.label}</div>
                <div
                  className={cn(
                    "text-xs font-medium uppercase tracking-[0.16em]",
                    active
                      ? "text-accent-foreground/[0.62]"
                      : completed
                        ? "text-success"
                        : "text-muted-foreground",
                  )}
                >
                  {statusLabel}
                </div>
              </div>
            </div>
            <div className="mt-5 flex items-end justify-between gap-3">
              <div className="min-w-0">
                <div
                  className={cn(
                    "text-xs leading-6",
                    active ? "text-accent-foreground/70" : "text-muted-foreground",
                  )}
                >
                  {step.detail}
                </div>
              </div>
              <div
                className={cn(
                  "h-1.5 w-16 rounded-full",
                  active
                    ? "bg-accent-foreground/70"
                    : completed
                      ? "bg-success/70"
                      : "bg-border",
                )}
              />
            </div>
          </div>
        );
      })}
    </div>
  );
}
