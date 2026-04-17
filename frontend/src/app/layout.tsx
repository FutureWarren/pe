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
    <html lang="en">
      <body className={`${publicSans.variable} ${ibmPlexMono.variable} antialiased`}>
        <DealsStoreProvider>
          <TopBar />
          <main className="mx-auto max-w-[1600px] px-4 py-6 sm:px-6 lg:px-8">{children}</main>
        </DealsStoreProvider>
      </body>
    </html>
  );
}
