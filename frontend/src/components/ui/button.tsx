import * as React from "react";

import { cva, type VariantProps } from "class-variance-authority";

import { cn } from "@/lib/utils";

const buttonVariants = cva(
  "inline-flex items-center justify-center gap-2 rounded-xl border text-sm font-semibold transition-[transform,background-color,border-color,color,box-shadow] duration-200 ease-out focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2 focus-visible:ring-offset-background active:translate-y-0 active:scale-[0.99] motion-reduce:transform-none disabled:pointer-events-none disabled:opacity-50",
  {
    variants: {
      variant: {
        default:
          "border-accent bg-accent text-accent-foreground shadow-[0_12px_28px_rgba(31,57,80,0.18)] hover:-translate-y-0.5 hover:bg-accent-strong",
        secondary:
          "border-border bg-surface-strong text-foreground hover:border-border-strong hover:bg-white",
        ghost:
          "border-transparent bg-transparent text-muted-foreground hover:bg-white/70 hover:text-foreground",
        outline:
          "border-border-strong bg-transparent text-foreground hover:bg-surface-strong",
        destructive:
          "border-danger/30 bg-danger/10 text-danger hover:bg-danger/15",
      },
      size: {
        default: "h-10 px-4 py-2",
        sm: "h-9 px-3 py-2 text-xs",
        lg: "h-11 px-5 py-2",
      },
    },
    defaultVariants: {
      variant: "default",
      size: "default",
    },
  },
);

export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {
  asChild?: boolean;
}

const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant, size, asChild = false, children, ...props }, ref) => {
    const classes = cn(buttonVariants({ variant, size }), className);

    if (asChild && React.isValidElement<{ className?: string }>(children)) {
      return React.cloneElement(children, {
        ...props,
        className: cn(classes, children.props.className),
      });
    }

    return (
      <button
        className={classes}
        ref={ref}
        {...props}
      >
        {children}
      </button>
    );
  },
);

Button.displayName = "Button";

export { Button, buttonVariants };
