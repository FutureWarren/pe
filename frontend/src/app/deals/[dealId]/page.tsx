"use client";

import { useParams } from "next/navigation";

import { DealNotFoundState } from "@/components/deals/deal-not-found-state";
import { OverviewView } from "@/components/deals/overview-view";
import { useDealById } from "@/lib/deals-store";

export default function DealOverviewPage() {
  const params = useParams<{ dealId: string }>();
  const deal = useDealById(params.dealId);

  if (!deal) {
    return <DealNotFoundState />;
  }

  return <OverviewView deal={deal} />;
}
