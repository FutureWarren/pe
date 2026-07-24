import type { Metadata } from "next";
import { IBM_Plex_Mono, Public_Sans } from "next/font/google";

import { TopBar } from "@/components/layout/top-bar";
import { DealsStoreProvider } from "@/lib/deals-store";

import "./globals.css";

const publicSans = Public_Sans({
  subsets: ["latin"],
  variable: "--font-public-sans",
});

const ibmPlexMono = IBM_Plex_Mono({
  subsets: ["latin"],
  weight: ["400", "500", "600"],
  variable: "--font-ibm-plex-mono",
});

export const metadata: Metadata = {
  title: {
    default: "Angelic Dataroom",
    template: "%s | Angelic",
  },
  description:
    "Import source files, extract financial data, apply deterministic formulas, and export a clean databook.",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    // suppressHydrationWarning: the no-FOUC script mutates the html class
    // before React hydrates, which is expected to differ from the server HTML.
    <html lang="en" suppressHydrationWarning>
      <head>
        {/* Apply the stored (or OS-preferred) theme before first paint so dark
            mode never flashes light. Kept inline and tiny on purpose. */}
        <script
          dangerouslySetInnerHTML={{
            __html:
              'try{var t=localStorage.getItem("angelic-theme");if(t==="dark"||(!t&&matchMedia("(prefers-color-scheme: dark)").matches))document.documentElement.classList.add("dark")}catch(e){}',
          }}
        />
      </head>
      <body className={`${publicSans.variable} ${ibmPlexMono.variable} antialiased`}>
        <DealsStoreProvider>
          <TopBar />
          <main className="mx-auto max-w-[1600px] px-4 py-6 sm:px-6 lg:px-8">{children}</main>
        </DealsStoreProvider>
      </body>
    </html>
  );
}
