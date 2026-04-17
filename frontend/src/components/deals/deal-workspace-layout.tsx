"use client";

import { ReactNode } from "react";

import { DealNotFoundState } from "@/components/deals/deal-not-found-state";
import { WorkspaceShell } from "@/components/deals/workspace-shell";
import { useDealById } from "@/lib/deals-store";

interface DealWorkspaceLayoutProps {
  dealId: string;
  children: ReactNode;
}

export function DealWorkspaceLayout({ dealId, children }: DealWorkspaceLayoutProps) {
  const deal = useDealById(dealId);

  if (!deal) {
    return <DealNotFoundState />;
  }

  return <WorkspaceShell deal={deal}>{children}</WorkspaceShell>;
}
