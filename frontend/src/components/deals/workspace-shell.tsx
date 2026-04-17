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
    <div className="grid gap-6 xl:grid-cols-[280px_minmax(0,1fr)_320px]">
      <WorkspaceSidebar deal={deal} focusedFileId={focusedFileId} />
      <div className="min-w-0 space-y-6">{children}</div>
      <div className="xl:block">
        <CopilotPanel deal={deal} onLocateFile={setFocusedFileId} />
      </div>
    </div>
  );
}
