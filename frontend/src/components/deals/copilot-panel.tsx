"use client";

import { FormEvent, useState, useTransition } from "react";

import { BotMessageSquare, LocateFixed, MessageSquareMore, SendHorizonal } from "lucide-react";

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Textarea } from "@/components/ui/textarea";
import { getCopilotReply, CopilotCitation } from "@/lib/copilot";
import { Deal } from "@/lib/types";
import { cn } from "@/lib/utils";

interface CopilotPanelProps {
  deal: Deal;
  onLocateFile?: (fileId: string) => void;
}

interface ChatMessage {
  id: string;
  role: "assistant" | "user";
  content: string;
  citations: CopilotCitation[];
}

function buildWelcomeMessage(): ChatMessage {
  return {
    id: "assistant-welcome",
    role: "assistant",
    content:
      "Ask where a number came from, why it was mapped a certain way, or which review item is blocking outputs. I will answer with the closest source support already visible in this workspace.",
    citations: [],
  };
}

export function CopilotPanel({ deal, onLocateFile }: CopilotPanelProps) {
  const [messages, setMessages] = useState<ChatMessage[]>([buildWelcomeMessage()]);
  const [draft, setDraft] = useState("");
  const [isPending, startTransition] = useTransition();

  const submitPrompt = (value: string) => {
    const prompt = value.trim();

    if (!prompt) {
      return;
    }

    const reply = getCopilotReply(deal, prompt);

    startTransition(() => {
      setMessages((current) => [
        ...current,
        {
          id: `user-${current.length + 1}`,
          role: "user",
          content: prompt,
          citations: [],
        },
        {
          id: `assistant-${current.length + 2}`,
          role: "assistant",
          content: reply.text,
          citations: reply.citations,
        },
      ]);
      setDraft("");
    });
  };

  const handleSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    submitPrompt(draft);
  };

  return (
    <Card className="sticky top-24">
      <CardHeader>
        <div className="flex items-center justify-between gap-3">
          <div>
            <CardDescription className="uppercase tracking-[0.18em]">
              Supportive AI copilot
            </CardDescription>
            <CardTitle className="mt-2 text-xl">Ask about traceability and blockers</CardTitle>
          </div>
          <div className="flex h-11 w-11 items-center justify-center rounded-2xl border border-border bg-white/90">
            <BotMessageSquare className="h-5 w-5 text-accent" />
          </div>
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        <Badge tone="muted">Secondary panel only</Badge>
        <p className="text-sm leading-6 text-muted-foreground">
          The assistant stays supportive. It can explain a mapping choice, summarize exceptions,
          or point you back to the file that currently supports a number.
        </p>

        <div className="flex flex-wrap gap-2">
          {deal.copilotPrompts.map((prompt) => (
            <Button
              key={prompt.id}
              type="button"
              variant="secondary"
              size="sm"
              className="h-auto min-h-9 justify-start whitespace-normal text-left"
              onClick={() => submitPrompt(prompt.label)}
            >
              <MessageSquareMore className="h-4 w-4 shrink-0" />
              {prompt.label}
            </Button>
          ))}
        </div>

        <div className="max-h-[420px] space-y-3 overflow-y-auto pr-1">
          {messages.map((message) => (
            <div
              key={message.id}
              className={cn(
                "space-y-2",
                message.role === "user" ? "ml-6" : "mr-4",
              )}
            >
              <div
                className={cn(
                  "rounded-2xl border px-4 py-3 text-sm leading-7",
                  message.role === "user"
                    ? "border-accent bg-accent text-accent-foreground"
                    : "border-border bg-white/90 text-foreground",
                )}
              >
                {message.content}
              </div>

              {message.citations.length > 0 ? (
                <div className="flex flex-wrap gap-2">
                  {message.citations.map((citation, index) => (
                    <Button
                      key={`${message.id}-${citation.fileId}-${index}`}
                      type="button"
                      size="sm"
                      variant="outline"
                      className="h-auto min-h-9 justify-start whitespace-normal text-left"
                      onClick={() => onLocateFile?.(citation.fileId)}
                    >
                      <LocateFixed className="h-4 w-4 shrink-0" />
                      <span className="space-y-0.5">
                        <span className="block text-xs font-semibold uppercase tracking-[0.14em] text-muted-foreground">
                          {citation.fileName}
                        </span>
                        <span className="block text-xs text-foreground">
                          {citation.locator} • {citation.note}
                        </span>
                      </span>
                    </Button>
                  ))}
                </div>
              ) : null}
            </div>
          ))}
        </div>

        <form className="space-y-3" onSubmit={handleSubmit}>
          <Textarea
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            placeholder="Ask where EBITDA came from, why churn was flagged, or which file supports a mapped value."
            className="min-h-[104px]"
          />
          <div className="flex items-center justify-between gap-3">
            <p className="text-xs leading-5 text-muted-foreground">
              Mock responses only. Citations locate files already connected to this deal.
            </p>
            <Button type="submit" size="sm" disabled={isPending || draft.trim().length === 0}>
              {isPending ? "Thinking..." : "Send"}
              <SendHorizonal className="h-4 w-4" />
            </Button>
          </div>
        </form>
      </CardContent>
    </Card>
  );
}
