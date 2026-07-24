import { ReactNode } from "react";

import { Badge } from "@/components/ui/badge";

interface PageIntroProps {
  eyebrow?: string;
  title: string;
  description: string;
  actions?: ReactNode;
  /** Extra badge next to the eyebrow (e.g. a "Sample" marker for demo deals). */
  badge?: ReactNode;
}

export function PageIntro({ eyebrow, title, description, actions, badge }: PageIntroProps) {
  return (
    <div className="flex flex-col gap-5 lg:flex-row lg:items-end lg:justify-between">
      <div className="max-w-3xl space-y-3">
        {eyebrow || badge ? (
          <div className="flex flex-wrap items-center gap-2">
            {eyebrow ? <Badge tone="accent">{eyebrow}</Badge> : null}
            {badge}
          </div>
        ) : null}
        <div className="space-y-2">
          <h1 className="text-3xl font-semibold tracking-tight text-foreground sm:text-4xl">
            {title}
          </h1>
          <p className="max-w-2xl text-sm leading-7 text-muted-foreground sm:text-base">
            {description}
          </p>
        </div>
      </div>
      {actions ? <div className="flex flex-wrap items-center gap-3">{actions}</div> : null}
    </div>
  );
}
