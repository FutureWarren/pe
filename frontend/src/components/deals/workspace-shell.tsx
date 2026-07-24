"use client";

import { ReactNode, useState } from "react";

import { CopilotPanel } from "@/components/deals/copilot-panel";
import { WorkspaceSidebar } from "@/components/deals/workspace-sidebar";
import { Deal } from "@/lib/types";

interface WorkspaceShellProps {
  deal: Deal;
  children: ReactNode;
}

export function WorkspaceShell({ deal, children }: WorkspaceShellProps) {
  const [focusedFileId, setFocusedFileId] = useState<string | null>(null);

  return (
    // Two columns at xl, three only at 2xl: squeezing sidebar + content +
    // copilot into 1280px left ~570px for the main tables. On mobile the
    // content comes first — the tall sidebar otherwise pushes it below the fold.
    <div className="grid gap-6 xl:grid-cols-[280px_minmax(0,1fr)] 2xl:grid-cols-[280px_minmax(0,1fr)_320px]">
      <div className="order-2 xl:order-1">
        <WorkspaceSidebar deal={deal} focusedFileId={focusedFileId} />
      </div>
      <div className="order-1 min-w-0 space-y-6 xl:order-2">{children}</div>
      <div className="order-3 hidden 2xl:block">
        <CopilotPanel deal={deal} onLocateFile={setFocusedFileId} />
      </div>
    </div>
  );
}
