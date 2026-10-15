import type { Config } from "tailwindcss";

// White and blue only. `ink-*` are kept as light-surface aliases so shared components stay consistent.
const config: Config = {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        canvas: "#f5f7fb",
        line: "#dce4f0",
        navy: "#0b1f4b",
        brand: { DEFAULT: "#1d4ed8", dark: "#1e3a8a", soft: "#eaf1ff", mid: "#dbe7ff", cyan: "#0ea5e9", violet: "#3b5bdb" },
        ink: { 950: "#f5f7fb", 900: "#ffffff", 850: "#ffffff", 800: "#f3f6fb", 700: "#e8eef8", 600: "#d7e1f0" },
      },
      fontFamily: {
        sans: ["var(--font-inter)", "ui-sans-serif", "Segoe UI", "system-ui", "sans-serif"],
        mono: ["ui-monospace", "Cascadia Code", "Consolas", "monospace"],
      },
      boxShadow: {
        panel: "0 1px 2px rgba(15,27,51,0.04), 0 8px 24px -12px rgba(15,27,51,0.10)",
        lift: "0 2px 4px rgba(15,27,51,0.05), 0 16px 40px -16px rgba(29,78,216,0.28)",
      },
    },
  },
  plugins: [],
};
export default config;