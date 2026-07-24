import * as React from "react";

import { cn } from "@/lib/utils";

const Input = React.forwardRef<HTMLInputElement, React.ComponentProps<"input">>(
  ({ className, ...props }, ref) => {
    return (
      <input
        className={cn(
          "flex h-11 w-full rounded-xl border border-border bg-white/80 px-3 py-2 text-sm text-foreground shadow-sm outline-none transition-[border-color,background-color,box-shadow] duration-200 focus:border-border-strong focus:bg-white focus-visible:ring-2 focus-visible:ring-accent/60",
          className,
        )}
        ref={ref}
        {...props}
      />
    );
  },
);

Input.displayName = "Input";

export { Input };
