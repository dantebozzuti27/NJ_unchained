import type { Metadata } from "next";
import Link from "next/link";

import "./globals.css";

export const metadata: Metadata = {
  title: "NJ Unchained — housing affordability + civic integrity",
  description:
    "New Jersey housing-affordability tracker and civic-integrity screener: " +
    "county-level burden divergence (FHFA HPI vs ACS real income) plus " +
    "cross-source risk signals from FEC, USAspending, HHS-OIG LEIE, and SAM.gov.",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body className="min-h-screen flex flex-col">
        <header className="border-b border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900">
          <div className="mx-auto max-w-6xl px-4 py-3 flex items-center justify-between">
            <Link
              href="/"
              className="font-mono text-lg font-semibold tracking-tight"
            >
              <span className="text-red-600 dark:text-red-400">NJ</span>{" "}
              Unchained
            </Link>
            <nav className="flex gap-4 text-sm">
              <Link
                href="/housing"
                className="hover:underline underline-offset-4"
              >
                Housing
              </Link>
              <Link
                href="/personalize"
                className="hover:underline underline-offset-4 font-semibold text-red-600 dark:text-red-400"
              >
                Personalize
              </Link>
              <Link
                href="/risk"
                className="hover:underline underline-offset-4"
              >
                Risk queue
              </Link>
              <Link
                href="/about"
                className="hover:underline underline-offset-4"
              >
                Methodology
              </Link>
            </nav>
          </div>
        </header>

        <main className="flex-1 mx-auto max-w-6xl w-full px-4 py-6">
          {children}
        </main>

        <footer className="border-t border-zinc-200 dark:border-zinc-800 mt-12 py-6 text-xs text-zinc-500 dark:text-zinc-400">
          <div className="mx-auto max-w-6xl px-4 flex flex-wrap gap-4 justify-between">
            <span>
              Data: FEC, USAspending.gov, HHS-OIG LEIE, SAM.gov, ACS, FHFA.
            </span>
            <span>
              Risk score is a percentile-of-anomalousness, NOT a probability of fraud.
              See{" "}
              <Link href="/about" className="underline">
                methodology
              </Link>
              .
            </span>
          </div>
        </footer>
      </body>
    </html>
  );
}
