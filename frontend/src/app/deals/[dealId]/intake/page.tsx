"use client";

import { useParams } from "next/navigation";

import { DealNotFoundState } from "@/components/deals/deal-not-found-state";
import { NewDealIntake } from "@/components/deals/new-deal-intake";
import { useDealById } from "@/lib/deals-store";

export default function DealIntakePage() {
  const params = useParams<{ dealId: string }>();
  const deal = useDealById(params.dealId);

  if (!deal) {
    return <DealNotFoundState />;
  }

  return <NewDealIntake deal={deal} />;
}
