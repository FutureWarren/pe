"use client";

import { useParams } from "next/navigation";

import { DataroomReviewView } from "@/components/dataroom/dataroom-review-view";

export default function ProcessReviewPage() {
  const params = useParams<{ dealId: string }>();

  return <DataroomReviewView dealId={params.dealId} />;
}
