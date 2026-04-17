"use client";

import { useParams } from "next/navigation";

import { DealNotFoundState } from "@/components/deals/deal-not-found-state";
import { ExtractionView } from "@/components/deals/extraction-view";
import { useDealById } from "@/lib/deals-store";

export default function ExtractionPage() {
  const params = useParams<{ dealId: string }>();
  const deal = useDealById(params.dealId);

  if (!deal) {
    return <DealNotFoundState />;
  }

  return <ExtractionView deal={deal} />;
}
