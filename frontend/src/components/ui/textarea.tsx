import * as React from "react";

import { cn } from "@/lib/utils";

const Textarea = React.forwardRef<HTMLTextAreaElement, React.ComponentProps<"textarea">>(
  ({ className, ...props }, ref) => {
    return (
      <textarea
        className={cn(
          "flex min-h-28 w-full rounded-xl border border-border bg-white/80 px-3 py-3 text-sm text-foreground shadow-sm outline-none transition-[border-color,background-color,box-shadow] duration-200 focus:border-border-strong focus:bg-white focus-visible:ring-2 focus-visible:ring-accent/60",
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
