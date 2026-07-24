import { cva, type VariantProps } from "class-variance-authority";

import { cn } from "@/lib/utils";

const badgeVariants = cva(
  "inline-flex items-center gap-1 rounded-full border px-2.5 py-1 text-[11px] font-semibold uppercase tracking-[0.16em] transition-colors duration-200",
  {
    variants: {
      tone: {
        default: "border-border bg-surface-muted text-foreground",
        muted: "border-border bg-white/70 text-muted-foreground",
        success: "border-success/20 bg-success/10 text-success",
        warning: "border-warning/20 bg-warning/10 text-warning",
        danger: "border-danger/20 bg-danger/10 text-danger",
        accent: "border-accent/15 bg-accent/10 text-accent",
      },
    },
    defaultVariants: {
      tone: "default",
    },
  },
);

interface BadgeProps extends React.HTMLAttributes<HTMLDivElement>, VariantProps<typeof badgeVariants> {}

export function Badge({ className, tone, ...props }: BadgeProps) {
  return <div className={cn(badgeVariants({ tone }), className)} {...props} />;
}
