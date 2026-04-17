import { DealWorkspaceLayout } from "@/components/deals/deal-workspace-layout";

export default async function DealLayout({
  children,
  params,
}: {
  children: React.ReactNode;
  params: Promise<{ dealId: string }>;
}) {
  const { dealId } = await params;

  return <DealWorkspaceLayout dealId={dealId}>{children}</DealWorkspaceLayout>;
}
