import * as React from "react";

import { cn } from "@/lib/utils";

const Select = React.forwardRef<HTMLSelectElement, React.ComponentProps<"select">>(
  ({ className, ...props }, ref) => {
    return (
      <select
        className={cn(
          "flex h-10 w-full rounded-xl border border-border bg-white/80 px-3 py-2 text-sm text-foreground shadow-sm outline-none transition focus:border-border-strong focus:bg-white focus:ring-2 focus:ring-accent/10",
          className,
        )}
        ref={ref}
        {...props}
      />
    );
  },
);

Select.displayName = "Select";

export { Select };
