import * as React from "react";

import { cn } from "@/lib/utils";

const Textarea = React.forwardRef<HTMLTextAreaElement, React.ComponentProps<"textarea">>(
  ({ className, ...props }, ref) => {
    return (
      <textarea
        className={cn(
          "flex min-h-28 w-full rounded-xl border border-border bg-white/80 px-3 py-3 text-sm text-foreground shadow-sm outline-none transition focus:border-border-strong focus:bg-white focus:ring-2 focus:ring-accent/10",
          className,
        )}
        ref={ref}
        {...props}
      />
    );
  },
);

Textarea.displayName = "Textarea";

export { Textarea };
