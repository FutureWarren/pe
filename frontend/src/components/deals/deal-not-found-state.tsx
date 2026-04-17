import Link from "next/link";

import { ArrowLeft } from "lucide-react";

import { buttonVariants } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { cn } from "@/lib/utils";

export function DealNotFoundState() {
  return (
    <Card className="mx-auto max-w-2xl">
      <CardHeader>
        <CardTitle>Deal not found</CardTitle>
        <CardDescription>
          The local workspace could not find this deal in the browser store.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <p className="text-sm leading-7 text-muted-foreground">
          This can happen if local browser state was cleared or if the deal was never created in
          this device session.
        </p>
        <Link href="/" className={cn(buttonVariants({ variant: "secondary" }))}>
          <ArrowLeft className="h-4 w-4" />
          Back to Deals
        </Link>
      </CardContent>
    </Card>
  );
}
