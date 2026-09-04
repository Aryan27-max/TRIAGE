import type { Config } from "tailwindcss";

// Two colour systems that must not bleed into each other (research/08 §8.3).
// `rzp-*` is the checkout replica; `ink-*` and `tri-*` are TRIAGE's own identity.
const config: Config = {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}", "./lib/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // Checkout pane — Razorpay-matched
        "rzp-blue": "#0B57E3",
        "rzp-blue-deep": "#0A3FB0",
        "rzp-blue-border": "#1668E3",
        "rzp-surface": "#FFFFFF",
        "rzp-method-rest": "#FFFFFF",
        "rzp-method-active": "#EEF4FF",
        "rzp-offer-bg": "#E7F7EE",
        "rzp-offer-fg": "#0A6B3D",
        "rzp-ink": "#16223A",
        "rzp-ink-dim": "#5A6B85",
        "rzp-cta": "#0F0F0F",
        // Inspector pane — TRIAGE identity
        "ink-900": "#10151C",
        "ink-800": "#171E28",
        "ink-700": "#202A36",
        "ink-line": "#2B3644",
        fg: "#E4EAF2",
        "fg-dim": "#7D8CA1",
        // The triage scale — the one idea running through every screen
        "tri-immediate": "#E5484D",
        "tri-delayed": "#F5A524",
        "tri-minor": "#2FA84F",
        "tri-expectant": "#5C6470",
        "tri-merchant": "#8892A4",
      },
      fontFamily: {
        sans: ["var(--font-inter)", "ui-sans-serif", "system-ui", "sans-serif"],
        mono: ["var(--font-mono)", "ui-monospace", "SFMono-Regular", "monospace"],
      },
      fontSize: {
        // 1.25 ratio, per §8.3. Body 15, data rows 13.
        xs: ["12px", "16px"],
        sm: ["13px", "18px"],
        base: ["15px", "22px"],
        lg: ["19px", "26px"],
        xl: ["24px", "30px"],
        "2xl": ["30px", "36px"],
      },
      borderRadius: {
        // Radius steps by hierarchy rather than being uniform.
        shell: "16px",
        card: "12px",
        control: "8px",
      },
      keyframes: {
        rowIn: {
          "0%": { opacity: "0", transform: "translateY(4px)" },
          "100%": { opacity: "1", transform: "translateY(0)" },
        },
        rowInFlat: { "0%": { opacity: "0" }, "100%": { opacity: "1" } },
      },
      animation: {
        // 120ms, opacity + a 4px lift. Staggered per row by an inline delay.
        "row-in": "rowIn 120ms ease-out both",
        "row-in-flat": "rowInFlat 120ms ease-out both",
      },
    },
  },
  plugins: [],
};
export default config;
