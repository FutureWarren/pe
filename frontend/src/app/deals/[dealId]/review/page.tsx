"use client";

import { useParams } from "next/navigation";

import { DealNotFoundState } from "@/components/deals/deal-not-found-state";
import { ReviewQueueView } from "@/components/deals/review-queue";
import { useDealById } from "@/lib/deals-store";

export default function ReviewPage() {
  const params = useParams<{ dealId: string }>();
  const deal = useDealById(params.dealId);

  if (!deal) {
    return <DealNotFoundState />;
  }

  return <ReviewQueueView deal={deal} />;
}
