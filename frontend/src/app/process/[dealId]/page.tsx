"use client";

import { useParams } from "next/navigation";

import { DataroomProcessView } from "@/components/dataroom/dataroom-process-view";

export default function ProcessPage() {
  const params = useParams<{ dealId: string }>();

  return <DataroomProcessView dealId={params.dealId} />;
}
