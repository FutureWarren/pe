"use client";

import { useEffect, useState } from "react";

import { Moon, Sun } from "lucide-react";

import { Button } from "@/components/ui/button";

const STORAGE_KEY = "angelic-theme";

/** Light/dark switcher. The initial class is applied before paint by the
 *  no-FOUC script in the root layout; this component only reads and toggles. */
export function ThemeToggle() {
  // null until mounted — the server can't know the user's stored preference,
  // so rendering the icon before hydration would mismatch.
  const [theme, setTheme] = useState<"light" | "dark" | null>(null);

  useEffect(() => {
    setTheme(document.documentElement.classList.contains("dark") ? "dark" : "light");
  }, []);

  const toggle = () => {
    const next = document.documentElement.classList.contains("dark") ? "light" : "dark";
    document.documentElement.classList.toggle("dark", next === "dark");
    try {
      window.localStorage.setItem(STORAGE_KEY, next);
    } catch {
      // Private-mode storage failures shouldn't break the toggle.
    }
    setTheme(next);
  };

  return (
    <Button
      type="button"
      variant="ghost"
      size="sm"
      aria-label={theme === "dark" ? "Switch to light mode" : "Switch to dark mode"}
      onClick={toggle}
    >
      {theme === "dark" ? (
        <Sun aria-hidden="true" className="h-4 w-4" />
      ) : (
        <Moon aria-hidden="true" className="h-4 w-4" />
      )}
    </Button>
  );
}
