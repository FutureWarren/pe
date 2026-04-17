"use client";

import { useParams } from "next/navigation";

import { DealNotFoundState } from "@/components/deals/deal-not-found-state";
import { MappingStudioView } from "@/components/deals/mapping-studio";
import { useDealById } from "@/lib/deals-store";

export default function MappingPage() {
  const params = useParams<{ dealId: string }>();
  const deal = useDealById(params.dealId);

  if (!deal) {
    return <DealNotFoundState />;
  }

  return <MappingStudioView deal={deal} />;
}
