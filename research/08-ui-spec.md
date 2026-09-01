# 8. UI Specification

Design spec for the TRIAGE demo interface. **This is a Stage 5 document.** Nothing here
gets built until Stage 3 ships — see `07-build-plan.md` and the anti-patterns table in
`CLAUDE.md`. Speccing now means Stage 5 becomes assembly rather than design under deadline.

---

## 8.1 The concept

Two panes, two audiences, one screen.

```
┌────────────────────────────────┬─────────────────────────────────────┐
│  CHECKOUT                      │  INSPECTOR                          │
│  Razorpay replica              │  What the merchant never sees       │
│  ~40%                          │  ~60%                               │
│                                │                                     │
│  Establishes credibility.      │  Carries the argument.              │
│  A judge recognises it in      │  Every decision, timed, sourced,    │
│  under a second.               │  and traceable to a policy rule.    │
└────────────────────────────────┴─────────────────────────────────────┘
```

The left pane exists so nobody questions whether you understand the domain. The right pane
is the actual product. Neither works alone: a replica by itself is a UI exercise, and an
inspector by itself has no context.

**The brief pins the left pane exactly** — it should read as Razorpay's checkout. The right
pane is free design space, and is where the visual identity of TRIAGE lives.

---

## 8.2 Asset boundary

Practical, not legal advice — but worth thirty seconds of thought given the submission
goes *to* Razorpay.

| Asset | Decision |
|---|---|
| Razorpay wordmark / logo | **Do not ship.** Replace with a TRIAGE mark in the same position and optical weight. |
| Layout, spacing, colour, type rhythm | **Match closely.** This is the "I studied your product" signal and it is the point. |
| Visa / Mastercard / RuPay / UPI app marks | Use neutral text chips (`VISA`, `UPI`, `NB`) or simple geometric glyphs. Trademarked logos add risk for zero demo value. |
| Bank names (SBI, HDFC, ICICI) | Fine as plain text — they are factual references in a simulated payment context. |
| "Secured by Razorpay" footer | Replace with `Simulated environment · TRIAGE`. Also prevents any confusion that this is a live payment. |

Sample the exact blues from your own screenshots with a colour picker. Values below are
close approximations read off the images, not sampled values — **verify before building.**

---

## 8.3 Design plan

### Colour

Two systems that must not bleed into each other.

**Checkout pane — Razorpay-matched**

| Token | Approx. hex | Use |
|---|---|---|
| `rzp-blue` | `#0B57E3` | Left panel base |
| `rzp-blue-deep` | `#0A3FB0` | Gradient terminus, bottom-right |
| `rzp-blue-border` | `#1668E3` | The 4px modal frame |
| `rzp-surface` | `#FFFFFF` | Right side of the modal |
| `rzp-method-rest` | `#FFFFFF` | Unselected method row |
| `rzp-method-active` | `#EEF4FF` | Selected method row |
| `rzp-offer-bg` | `#E7F7EE` | Cashback chip background |
| `rzp-offer-fg` | `#0A6B3D` | Cashback chip text |
| `rzp-ink` | `#16223A` | Primary text |
| `rzp-ink-dim` | `#5A6B85` | Secondary text |
| `rzp-cta` | `#0F0F0F` | Continue button |

**Inspector pane — TRIAGE identity**

Deep slate rather than near-black, because near-black-plus-one-bright-accent is the
default every generated dashboard reaches for. The accent here isn't one colour; it's the
five-part triage scale, and it comes from the subject matter rather than from a palette
generator.

| Token | Hex | Use |
|---|---|---|
| `ink-900` | `#10151C` | Inspector background |
| `ink-800` | `#171E28` | Row background, panels |
| `ink-700` | `#202A36` | Expanded row, hover |
| `ink-line` | `#2B3644` | Hairlines, the timeline rail |
| `fg` | `#E4EAF2` | Primary text |
| `fg-dim` | `#7D8CA1` | Timestamps, units, secondary |

**The triage scale — the one idea that runs through every screen**

Derived from medical triage categories, which is where the project's name comes from.
Same five colours on the taxonomy board, the inspector rows, and the case list.

| Category | Action classes | Token | Hex | Treatment |
|---|---|---|---|---|
| Immediate | `RETRY_NOW`, `SWITCH_RAIL` | `tri-immediate` | `#E5484D` | Filled chip |
| Delayed | `RETRY_SCHEDULED`, `AWAIT_STATUS` | `tri-delayed` | `#F5A524` | Filled chip |
| Minor | `NUDGE_CUSTOMER` | `tri-minor` | `#2FA84F` | Filled chip |
| Expectant | `SWITCH_INSTRUMENT`, `STOP` | `tri-expectant` | `#5C6470` | **Hollow chip, 1px border** |
| Not a casualty | `MERCHANT_ALERT` | `tri-merchant` | `#8892A4` | Dashed border |

Expectant renders hollow rather than filled because true black is invisible on a dark
background — and the hollow treatment carries the meaning better anyway: nothing will be
spent on this one.

### Type

| Role | Face | Notes |
|---|---|---|
| Checkout pane | **Inter** | Closest free match to Razorpay's rhythm. `Mona Sans` (OFL, GitHub) is closer on letterforms if you want to swap. |
| Inspector UI | **Inter** | Same family — the panes are one product, not two. |
| Inspector data columns | **JetBrains Mono** | Timestamps, durations, JSON, feature vectors. |

Monospace is confined to columns where digits must align vertically. Labels, headings and
prose in the Inspector stay in Inter — monospace applied to UI labels is decoration, and
reads as costume.

Scale, 1.25 ratio: `12 / 13 / 15 / 19 / 24 / 30`. Body 15px. Data rows 13px. Sentence case
throughout; no tracked-out capitals.

### Space and shape

4px base unit. Steps: `4 8 12 16 24 32 48`.

| Element | Radius |
|---|---|
| Modal shell | 16px |
| Method rows, cards | 12px |
| Inputs, chips | 8px |
| Status pills | 999px |

Radius is not uniform across the interface — the shell, the cards, and the controls are
three different levels of hierarchy and read that way.

### Motion

Exactly two moments, both responses to something the user did:

1. **Method row expands** — 180ms, height and opacity.
2. **Inspector row appears** — 120ms, opacity and a 4px upward translate, staggered 40ms
   per row as the decision chain resolves.

Nothing animates on load. Nothing animates on hover beyond a background colour change.
Respect `prefers-reduced-motion`; when set, rows appear without the translate.

---

## 8.4 Checkout pane — component inventory

Scope tightly. This pane is a trigger, not a working checkout.

```
┌──────────────────────────────────────────────────────────┐
│ ▓▓ TRIAGE            Payment Options              ⋯   ✕  │
│ ▓▓ Simulated                                             │
│ ▓▓                  ┌──────────────┬──────────────────┐  │
│ ▓▓ Price Summary    │ UPI       ▸  │                  │  │
│ ▓▓ ₹4,999           │ Cards        │  [expanded       │  │
│ ▓▓                  │ Netbanking   │   method panel]  │  │
│ ▓▓ Using as +91 …   │ Wallet       │                  │  │
│ ▓▓                  └──────────────┴──────────────────┘  │
│ ▓▓ Scenario ▾                                            │
└──────────────────────────────────────────────────────────┘
```

**Build:**

| Component | Notes |
|---|---|
| `CheckoutShell` | 4px `rzp-blue-border`, 16px radius, drop shadow |
| `MerchantPanel` | Left ~38%, blue gradient, TRIAGE mark, price, masked phone |
| `MethodList` | Four rows: UPI, Cards, Netbanking, Wallet. Selected row gets `rzp-method-active` and a left indicator |
| `MethodPanel` | Right side. One method's content at a time |
| `OfferChip` | Green pill — pure visual fidelity, no behaviour |
| `PayButton` | Black, full width, label matches the action |
| `ConfirmingOverlay` | The "Confirming Payment" state. **Needed** — it's the moment failure gets injected |

**Skip:** bank search, the wallet provider list, QR generation, card field validation,
"More options", Pay Later. A method needs to be *selectable*, not functional.

**Add — not in the original:**

`ScenarioPicker`, tucked behind the `⋯` menu, which is empty space in the real UI. Lets you
choose which of the 110 codes to inject, or fire a downtime event. This is what turns a
static replica into a demo instrument, and it's the control you'll actually drive from
during the video.

**Copy note:** the button says what happens. "Pay ₹4,999", not "Submit". If a payment
fails, the message states what happened and what comes next — "Your bank declined this.
TRIAGE is routing to card." No apology, no vagueness.

---

## 8.5 Inspector pane

The memorable element. Everything else on screen stays quiet so this can carry weight.

### Structure — a rail, not a table

Events pin to a vertical rail whose colour segments track the case state machine. A plain
table would show the same data; the rail shows the *shape* of the decision — where it
paused, where it branched, where it stopped.

```
INSPECTOR                                    case_NpQ4vR    ● LIVE

 │
 ●  14:02:01.204  payment.failed                          302ms
 │  bank_technical_error · source: bank · upi · ₹4,999
 │
 ●  14:02:01.208  policy.resolve                            4ms
 │  ▸ SWITCH_RAIL          [Immediate]
 │
 ●  14:02:01.220  rails.health                             12ms
 │  @oksbi · severity: high · started 14:00:11
 │
 ○  14:02:01.221  model.score                        not invoked
 │  action is model-eligible — suppressed: rail severity high
 │
 ●  14:02:01.227  executor.decide                           6ms
 │  ▸ route to card · attempt 1 of 4
 │
 ●  14:02:01.229  audit.write                               2ms
 ╵  RECEIVED → DIAGNOSED · key case_NpQ4vR:1
```

Every row expands to the real request/response JSON from `05-api-reference.md`. The
Inspector renders your actual API payloads — it does not invent a display format.

### The row that matters

`model.score` is the ML story in one line.

- On the 83 non-eligible codes it renders **hollow** (`○`) and says *not invoked*. Its
  absence proves I-1 — the model is not consulted where policy is final.
- On `RETRY_SCHEDULED` and `SWITCH_RAIL` it renders filled, expands to the feature vector,
  the predicted probability, and the top contributing features from LightGBM's importances.

Make this row visually distinct from the rest. It is the only place in the interface where
a model appears at all, and that restraint is the argument.

### Components

| Component | Notes |
|---|---|
| `InspectorRail` | The vertical line, segmented by case state |
| `EventRow` | Node, timestamp, event name, duration, one-line summary |
| `EventDetail` | Expanded JSON, syntax-highlit, copy button |
| `TriageChip` | The five-category chip, filled/hollow/dashed per the scale |
| `ModelRow` | Special-cased `EventRow` — feature vector and probability |
| `RailHealthStrip` | Persistent header band; goes amber/red on active downtime |

---

## 8.6 Screen inventory

Still four screens, as capped in `07-build-plan.md`. This replaces two of the originals.

| Screen | Contents | Demo weight |
|---|---|---|
| **Live** | Checkout + Inspector. This document. | ~3 of 5 minutes |
| **Taxonomy** | All 110 codes as a grid, coloured by triage category. Opens the demo. | ~45s |
| **Cases** | Batch history from simulator runs, filterable by arm and code | ~30s |
| **Results** | Arm comparison, per-code table including negative rows | ~60s |

The Taxonomy board is the visual form of the 27-of-110 finding. Grid, five colours, the
recoverable 27 clustered and visibly outnumbered. It should be legible in two seconds from
across a room.

---

## 8.7 Stack and materials

```bash
npx create-next-app@latest dashboard --ts --tailwind --app --eslint
cd dashboard
npm i lucide-react clsx
npm i -D @tailwindcss/typography
```

| Need | Choice | Why |
|---|---|---|
| Framework | Next.js App Router | Your stack, no learning cost |
| Styling | Tailwind | Token config maps 1:1 to §8.3 |
| Icons | `lucide-react` | Free, MIT, consistent weight |
| Fonts | `next/font/google` — Inter, JetBrains Mono | Self-hosted, no layout shift |
| JSON display | Hand-rolled `<pre>` + a small tokeniser | A syntax-highlighting library is 40kb for six colours |
| Charts (Results) | Plain SVG or `recharts` | Two bar charts. Don't over-tool. |
| State | `useState` + SWR polling `/v1/recovery/cases` | No state library for four screens |

**No component library.** shadcn/MUI/Chakra all carry a recognisable default look, and half
this interface is a deliberate replica of someone else's design. Their defaults would fight
you.

### Asset checklist

- [ ] Sample exact blues from your screenshots with a colour picker → fill §8.3 values
- [ ] TRIAGE wordmark — plain Inter 600 in white is entirely sufficient
- [ ] Method glyphs — simple geometric marks or text chips, not brand logos
- [ ] Favicon
- [ ] `tailwind.config.ts` extended with both token sets, namespaced `rzp-*` and `ink-*`/`tri-*`

---

## 8.8 Build order — Stage 5

Four hours. Sequenced so a demo exists even if the last item doesn't land.

| # | Task | ~Time |
|---|---|---|
| 1 | Tailwind token config, fonts, layout shell | 30m |
| 2 | Checkout pane — shell, merchant panel, method list | 60m |
| 3 | Inspector — rail, event rows, triage chips | 75m |
| 4 | Wire to the live API, scenario picker | 45m |
| 5 | Taxonomy board | 30m |
| 6 | Results screen | 30m |

**Cut order if short:** Results screen first (it's already in `eval/report.md` as markdown —
show that instead), then the Taxonomy board (a static image works), then Inspector row
expansion. **Never cut** the Inspector rail itself or the scenario picker — without those
two there's no live demo, only a screenshot.

---

## 8.9 What this deliberately avoids

Recorded so the choices are legible rather than accidental.

- **No warm-cream-and-serif treatment**, no acid-green-on-black. Both are the current
  generated-design defaults and neither has anything to do with payments.
- **No uniform card grid** with one radius and one grey shadow on everything. Radius steps
  by hierarchy; the Inspector uses a rail rather than cards.
- **No all-caps eyebrow labels**, no middle-dot meta strings, no arrows appended to buttons.
- **No monospace as texture** — it appears only where digits need to align.
- **No animation on load.** Motion answers an action or doesn't happen.
- **Boldness spent in one place:** the Inspector rail and the triage colour scale. The
  checkout pane is a faithful, quiet replica; the Results and Cases screens are plain
  tables. One memorable thing, everything else disciplined.
