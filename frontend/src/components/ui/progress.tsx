import * as React from "react";

import { cn } from "@/lib/utils";

interface ProgressProps {
  value: number;
  className?: string;
  /** Accessible name announced by screen readers (e.g. "Extraction progress"). */
  label?: string;
}

export function Progress({ value, className, label }: ProgressProps) {
  const clamped = Math.max(0, Math.min(100, value));
  return (
    <div
      className={cn("h-2 rounded-full bg-muted/60", className)}
      role="progressbar"
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={Math.round(clamped)}
      aria-label={label}
    >
      <div
        className="progress-fill h-full rounded-full bg-accent"
        style={{ "--progress-w": `${clamped}%` } as React.CSSProperties}
      />
    </div>
  );
}
