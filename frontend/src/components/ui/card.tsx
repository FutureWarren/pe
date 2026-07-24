import * as React from "react";

import { cn } from "@/lib/utils";

function Card({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn(
        // relative anchors the .lift-card hover-shadow pseudo-element.
        "relative rounded-2xl border border-border bg-surface p-5 shadow-card backdrop-blur-sm transition-[transform,border-color,background-color] duration-300",
        className,
      )}
      {...props}
    />
  );
}

function CardHeader({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("flex flex-col gap-1.5", className)} {...props} />;
}

interface CardTitleProps extends React.HTMLAttributes<HTMLHeadingElement> {
  /** Render element. Use "p" for KPI values so bare numbers don't pollute the
   *  document's heading outline for screen readers. */
  as?: "h2" | "h3" | "h4" | "p" | "div";
}

function CardTitle({ className, as: Tag = "h3", ...props }: CardTitleProps) {
  return <Tag className={cn("text-lg font-semibold tracking-tight", className)} {...props} />;
}

function CardDescription({ className, ...props }: React.HTMLAttributes<HTMLParagraphElement>) {
  return <p className={cn("text-sm leading-6 text-muted-foreground", className)} {...props} />;
}

function CardContent({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("mt-4", className)} {...props} />;
}

export { Card, CardContent, CardDescription, CardHeader, CardTitle };
