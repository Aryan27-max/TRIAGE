import type { Metadata } from "next";
import { Inter, JetBrains_Mono } from "next/font/google";
import "./globals.css";
import { Nav } from "@/components/Nav";
import { Faq } from "@/components/Faq";

const inter = Inter({ subsets: ["latin"], variable: "--font-inter", display: "swap" });
const mono = JetBrains_Mono({ subsets: ["latin"], variable: "--font-mono", display: "swap" });

export const metadata: Metadata = {
  title: "TRIAGE — the decision layer for payment failures",
  description:
    "Razorpay publishes 110 payment failure reasons. Only 27 are recoverable without " +
    "human intervention. TRIAGE is the missing decision layer.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={`${inter.variable} ${mono.variable}`}>
      <body className="min-h-screen bg-ink-900 font-sans text-fg antialiased">
        <Nav />
        <main className="mx-auto w-full max-w-[1600px] px-6 pb-16">{children}</main>
        <Faq />
      </body>
    </html>
  );
}
