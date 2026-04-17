import Link from "next/link";

import { ArrowRight, Building2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";

export function TopBar() {
  return (
    <header className="sticky top-0 z-40 border-b border-border bg-surface backdrop-blur-2xl">
      <div className="mx-auto flex max-w-[1600px] items-center justify-between gap-4 px-4 py-4 sm:px-6 lg:px-8">
        <div className="flex items-center gap-4">
          <Link className="group flex items-center gap-3" href="/">
            <div className="flex h-11 w-11 items-center justify-center rounded-2xl border border-border-strong bg-accent text-accent-foreground shadow-[0_16px_32px_rgba(31,57,80,0.22)] transition-transform duration-300 group-hover:-translate-y-0.5">
              <Building2 className="h-5 w-5" />
            </div>
            <div>
              <div className="text-[11px] font-semibold uppercase tracking-[0.22em] text-muted-foreground">
                Angelic
              </div>
              <div className="text-base font-semibold tracking-tight text-foreground transition-colors duration-300 group-hover:text-accent">
                Dataroom
              </div>
            </div>
          </Link>
          <Badge tone="accent" className="hidden md:inline-flex shadow-[0_10px_24px_rgba(31,57,80,0.08)]">
            Import to Workbook
          </Badge>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <Button asChild variant="outline" size="sm">
            <Link href="/history">历史记录</Link>
          </Button>
          <Button asChild size="sm" className="shadow-[0_16px_32px_rgba(31,57,80,0.2)]">
            <Link href="/">
              Start New Import
              <ArrowRight className="h-4 w-4" />
            </Link>
          </Button>
        </div>
      </div>
    </header>
  );
}
