"use client";

import { useState } from "react";
import clsx from "clsx";
import { Loader2, MoreHorizontal, X } from "lucide-react";
import type { ErrorPolicy } from "@/lib/api";
import { ScenarioPicker } from "./ScenarioPicker";

/**
 * A replica of Razorpay's checkout, built as a trigger rather than a working checkout.
 *
 * Asset boundary (research/08 §8.2): the layout, spacing, colour and type rhythm are
 * matched closely — that is the "I studied your product" signal and it is the point.
 * The wordmark is TRIAGE's, not Razorpay's; the method marks are text chips rather
 * than trademarked logos; the footer says this is simulated.
 */

const METHODS = [
  { id: "upi", label: "UPI", chip: "UPI", detail: "Pay by any UPI app" },
  { id: "card", label: "Cards", chip: "CARD", detail: "Visa, Mastercard, RuPay" },
  { id: "netbanking", label: "Netbanking", chip: "NB", detail: "All Indian banks" },
  { id: "wallet", label: "Wallet", chip: "W", detail: "PayZapp, Freecharge, Mobikwik" },
];

const HANDLES = ["@oksbi", "@ybl", "@paytm", "@okhdfcbank", "@okaxis", "@apl"];

export type CheckoutSubmission = {
  method: string;
  errorCode: string;
  vpaHandle: string;
  amountPaise: number;
};

export function Checkout({
  codes,
  onSubmit,
  onInjectDowntime,
  busy,
  lastOutcome,
}: {
  codes: ErrorPolicy[];
  onSubmit: (submission: CheckoutSubmission) => void;
  onInjectDowntime: (method: string, severity: string) => void;
  busy: boolean;
  lastOutcome: { action: string; message: string } | null;
}) {
  const [method, setMethod] = useState("upi");
  const [handle, setHandle] = useState("@oksbi");
  const [errorCode, setErrorCode] = useState("bank_technical_error");
  const [pickerOpen, setPickerOpen] = useState(false);
  const amountPaise = 499900;

  const selected = codes.find((c) => c.code === errorCode);

  return (
    <div className="relative overflow-hidden rounded-shell border-4 border-rzp-blue-border bg-rzp-surface shadow-2xl">
      <div className="flex min-h-[560px]">
        {/* Merchant panel — left ~38%, blue gradient */}
        <aside className="relative w-[38%] shrink-0 bg-gradient-to-br from-rzp-blue to-rzp-blue-deep p-6 text-white">
          <div className="flex items-baseline gap-2">
            <span className="text-lg font-semibold tracking-tight">TRIAGE</span>
          </div>
          <p className="mt-0.5 text-[11px] text-white/60">Simulated merchant</p>

          <div className="mt-8">
            <p className="text-[11px] uppercase tracking-wide text-white/50">
              Price summary
            </p>
            <p className="mt-1 text-2xl font-semibold">
              ₹{(amountPaise / 100).toLocaleString("en-IN")}
            </p>
          </div>

          <div className="mt-6 inline-flex items-center rounded-control bg-rzp-offer-bg px-2.5 py-1 text-xs font-medium text-rzp-offer-fg">
            ₹100 cashback on UPI
          </div>

          <div className="mt-8 border-t border-white/15 pt-4">
            <p className="text-[11px] text-white/50">Using as</p>
            <p className="mt-0.5 font-mono text-sm">+91 98••• ••210</p>
          </div>

          <button
            type="button"
            onClick={() => setPickerOpen((open) => !open)}
            className="absolute bottom-6 left-6 flex items-center gap-1.5 rounded-control border border-white/25 px-2.5 py-1.5 text-xs text-white/85 transition-colors hover:bg-white/10"
          >
            Scenario
            <span className="font-mono text-[11px] text-white/60">{errorCode}</span>
          </button>
        </aside>

        {/* Payment options */}
        <section className="relative flex-1">
          <header className="flex items-center justify-between border-b border-black/[0.06] px-5 py-3.5">
            <h2 className="text-base font-semibold text-rzp-ink">Payment Options</h2>
            <div className="flex items-center gap-1 text-rzp-ink-dim">
              <button
                type="button"
                aria-label="Scenario picker"
                onClick={() => setPickerOpen((open) => !open)}
                className={clsx(
                  "rounded-control p-1.5 transition-colors hover:bg-black/5",
                  pickerOpen && "bg-black/5 text-rzp-ink",
                )}
              >
                <MoreHorizontal size={18} />
              </button>
              <span className="rounded-control p-1.5 opacity-40">
                <X size={18} />
              </span>
            </div>
          </header>

          {pickerOpen && (
            <ScenarioPicker
              codes={codes}
              selected={errorCode}
              onSelect={(code) => {
                setErrorCode(code);
                setPickerOpen(false);
              }}
              onInjectDowntime={(m, s) => {
                onInjectDowntime(m, s);
                setPickerOpen(false);
              }}
              onClose={() => setPickerOpen(false)}
            />
          )}

          <div className="flex">
            <ul className="w-[42%] shrink-0 border-r border-black/[0.06] py-2">
              {METHODS.map((entry) => {
                const active = entry.id === method;
                return (
                  <li key={entry.id}>
                    <button
                      type="button"
                      onClick={() => setMethod(entry.id)}
                      className={clsx(
                        "relative flex w-full items-center gap-3 px-5 py-3 text-left transition-colors",
                        active
                          ? "bg-rzp-method-active"
                          : "bg-rzp-method-rest hover:bg-black/[0.02]",
                      )}
                    >
                      {active && (
                        <span className="absolute left-0 top-1/2 h-7 w-[3px] -translate-y-1/2 rounded-r bg-rzp-blue" />
                      )}
                      <span className="flex h-7 w-9 items-center justify-center rounded border border-black/10 bg-white font-mono text-[10px] font-semibold text-rzp-ink-dim">
                        {entry.chip}
                      </span>
                      <span>
                        <span className="block text-sm font-medium text-rzp-ink">
                          {entry.label}
                        </span>
                        <span className="block text-[11px] text-rzp-ink-dim">
                          {entry.detail}
                        </span>
                      </span>
                    </button>
                  </li>
                );
              })}
            </ul>

            {/* One method's panel at a time. Selectable, not functional. */}
            <div className="flex-1 p-5">
              {method === "upi" && (
                <div>
                  <p className="text-sm font-medium text-rzp-ink">Pay by UPI app</p>
                  <p className="mt-1 text-xs text-rzp-ink-dim">
                    A collect request goes to your app.
                  </p>
                  <div className="mt-4 flex flex-wrap gap-2">
                    {HANDLES.map((option) => (
                      <button
                        key={option}
                        type="button"
                        onClick={() => setHandle(option)}
                        className={clsx(
                          "rounded-control border px-2.5 py-1.5 font-mono text-xs transition-colors",
                          option === handle
                            ? "border-rzp-blue bg-rzp-method-active text-rzp-blue"
                            : "border-black/10 text-rzp-ink-dim hover:border-black/25",
                        )}
                      >
                        {option}
                      </button>
                    ))}
                  </div>
                </div>
              )}
              {method === "card" && (
                <div>
                  <p className="text-sm font-medium text-rzp-ink">Card details</p>
                  <div className="mt-4 space-y-2">
                    <div className="h-9 rounded-control border border-black/10 bg-black/[0.02]" />
                    <div className="flex gap-2">
                      <div className="h-9 flex-1 rounded-control border border-black/10 bg-black/[0.02]" />
                      <div className="h-9 w-24 rounded-control border border-black/10 bg-black/[0.02]" />
                    </div>
                  </div>
                </div>
              )}
              {method === "netbanking" && (
                <div>
                  <p className="text-sm font-medium text-rzp-ink">Select a bank</p>
                  <div className="mt-4 grid grid-cols-2 gap-2">
                    {["SBI", "HDFC", "ICICI", "Axis"].map((bank) => (
                      <div
                        key={bank}
                        className="rounded-control border border-black/10 px-3 py-2 text-sm text-rzp-ink-dim"
                      >
                        {bank}
                      </div>
                    ))}
                  </div>
                </div>
              )}
              {method === "wallet" && (
                <div>
                  <p className="text-sm font-medium text-rzp-ink">Select a wallet</p>
                  <div className="mt-4 space-y-2">
                    {["PayZapp", "Freecharge", "Mobikwik"].map((wallet) => (
                      <div
                        key={wallet}
                        className="rounded-control border border-black/10 px-3 py-2 text-sm text-rzp-ink-dim"
                      >
                        {wallet}
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {selected && (
                <p className="mt-6 rounded-control bg-black/[0.03] px-3 py-2 text-[11px] leading-relaxed text-rzp-ink-dim">
                  This attempt is scripted to fail with{" "}
                  <span className="font-mono text-rzp-ink">{selected.code}</span>.
                </p>
              )}
            </div>
          </div>

          <div className="absolute inset-x-0 bottom-0 border-t border-black/[0.06] p-5">
            {lastOutcome && (
              <p className="mb-3 text-xs leading-relaxed text-rzp-ink">
                {lastOutcome.message}
              </p>
            )}
            <button
              type="button"
              disabled={busy}
              onClick={() =>
                onSubmit({ method, errorCode, vpaHandle: handle, amountPaise })
              }
              className="w-full rounded-control bg-rzp-cta py-3 text-sm font-medium text-white transition-opacity hover:opacity-90 disabled:opacity-60"
            >
              {busy ? "Confirming payment…" : `Pay ₹${(amountPaise / 100).toLocaleString("en-IN")}`}
            </button>
            <p className="mt-3 text-center text-[11px] text-rzp-ink-dim">
              Simulated environment · TRIAGE
            </p>
          </div>
        </section>
      </div>

      {busy && (
        <div className="absolute inset-0 flex flex-col items-center justify-center gap-3 bg-white/85">
          <Loader2 className="animate-spin text-rzp-blue" size={28} />
          <p className="text-sm font-medium text-rzp-ink">Confirming payment</p>
          <p className="text-xs text-rzp-ink-dim">Do not press back</p>
        </div>
      )}
    </div>
  );
}
