import Link from "next/link";

import { buttonVariants } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { cn } from "@/lib/utils";

export default function NotFound() {
  return (
    <div className="mx-auto max-w-2xl py-16">
      <Card>
        <CardHeader>
          <CardTitle as="p" className="text-3xl tabular-nums">Page not found</CardTitle>
          <CardDescription>
            The requested deal workspace or output preview could not be found.
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-wrap gap-3">
          <Link className={cn(buttonVariants({ variant: "default" }))} href="/">
            Return to import home
          </Link>
          <Link className={cn(buttonVariants({ variant: "secondary" }))} href="/deals/new">
            Open new deal intake
          </Link>
        </CardContent>
      </Card>
    </div>
  );
}
