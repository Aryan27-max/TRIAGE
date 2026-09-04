"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import clsx from "clsx";
import { ApiStatus } from "./ApiStatus";

const SCREENS = [
  { href: "/", label: "Live" },
  { href: "/taxonomy", label: "Taxonomy" },
  { href: "/results", label: "Results" },
  { href: "/cases", label: "Cases" },
];

export function Nav() {
  const pathname = usePathname();
  return (
    <header className="sticky top-0 z-30 border-b border-ink-line bg-ink-900/95 backdrop-blur">
      <div className="mx-auto flex w-full max-w-[1600px] items-center gap-8 px-6 py-3">
        <Link href="/" className="flex items-baseline gap-2">
          <span className="text-lg font-semibold tracking-tight text-fg">TRIAGE</span>
          <span className="hidden text-xs text-fg-dim sm:inline">
            the decision layer for payment failures
          </span>
        </Link>
        <nav className="flex items-center gap-1">
          {SCREENS.map((screen) => {
            const active =
              screen.href === "/" ? pathname === "/" : pathname.startsWith(screen.href);
            return (
              <Link
                key={screen.href}
                href={screen.href}
                className={clsx(
                  "rounded-control px-3 py-1.5 text-sm transition-colors",
                  active
                    ? "bg-ink-700 text-fg"
                    : "text-fg-dim hover:bg-ink-800 hover:text-fg",
                )}
              >
                {screen.label}
              </Link>
            );
          })}
        </nav>
        <div className="ml-auto">
          <ApiStatus />
        </div>
      </div>
    </header>
  );
}
