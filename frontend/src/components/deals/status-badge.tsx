import { Badge } from "@/components/ui/badge";

interface StatusBadgeProps {
  value: string;
}

export function StatusBadge({ value }: StatusBadgeProps) {
  const normalized = value.toLowerCase();

  if (
    normalized.includes("ready") ||
    normalized.includes("approved") ||
    normalized.includes("output") ||
    normalized.includes("complete") ||
    normalized.includes("mapped")
  ) {
    return <Badge tone="success">{value}</Badge>;
  }

  if (
    normalized.includes("critical") ||
    normalized.includes("high") ||
    normalized.includes("flagged") ||
    normalized.includes("danger") ||
    normalized.includes("blocked")
  ) {
    return <Badge tone="danger">{value}</Badge>;
  }

  if (
    normalized.includes("review") ||
    normalized.includes("warning") ||
    normalized.includes("deferred") ||
    normalized.includes("partial")
  ) {
    return <Badge tone="warning">{value}</Badge>;
  }

  if (
    normalized.includes("mapping") ||
    normalized.includes("scan") ||
    normalized.includes("extract") ||
    normalized.includes("queued") ||
    normalized.includes("progress") ||
    normalized.includes("pending")
  ) {
    return <Badge tone="accent">{value}</Badge>;
  }

  if (normalized.includes("not started") || normalized.includes("unused")) {
    return <Badge tone="muted">{value}</Badge>;
  }

  return <Badge tone="muted">{value}</Badge>;
}
