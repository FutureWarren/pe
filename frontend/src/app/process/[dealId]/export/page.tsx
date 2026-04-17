"use client";

import { useParams } from "next/navigation";

import { DataroomExportView } from "@/components/dataroom/dataroom-export-view";

export default function ProcessExportPage() {
  const params = useParams<{ dealId: string }>();

  return <DataroomExportView dealId={params.dealId} />;
}
