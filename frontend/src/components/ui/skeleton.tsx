import { cn } from "@/lib/utils";

/** Shimmering loading placeholder. Size it with h-/w- utilities. */
export function Skeleton({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("skeleton", className)} aria-hidden="true" {...props} />;
}

/** Full-page placeholder matching the dataroom screens' hero + stat layout,
 *  shown while the deals store hydrates from localStorage. */
export function DataroomSkeleton() {
  return (
    <div className="page-shell space-y-8" role="status" aria-label="Loading">
      <Skeleton className="h-48 w-full rounded-[28px]" />
      <div className="grid gap-4 sm:grid-cols-3">
        <Skeleton className="h-24" />
        <Skeleton className="h-24" />
        <Skeleton className="h-24" />
      </div>
      <Skeleton className="h-64 w-full rounded-2xl" />
      <span className="sr-only">Loading…</span>
    </div>
  );
}
