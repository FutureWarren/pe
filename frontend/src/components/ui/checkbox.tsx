import * as React from "react";

import { cn } from "@/lib/utils";

const Checkbox = React.forwardRef<HTMLInputElement, React.ComponentProps<"input">>(
  ({ className, ...props }, ref) => {
    return (
      <input
        ref={ref}
        type="checkbox"
        className={cn(
          // accent-color is the only reliable way to theme a native checkbox —
          // color/border utilities are no-ops on the control itself.
          "h-4 w-4 accent-accent focus-visible:outline-2 focus-visible:outline-accent focus-visible:outline-offset-2",
          className,
        )}
        {...props}
      />
    );
  },
);

Checkbox.displayName = "Checkbox";

export { Checkbox };
