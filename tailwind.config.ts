import type { Config } from "tailwindcss";

const config: Config = {
  content: [
    "./app/**/*.{js,ts,jsx,tsx,mdx}",
    "./components/**/*.{js,ts,jsx,tsx,mdx}",
  ],
  theme: {
    extend: {
      fontFamily: {
        mono: ["ui-monospace", "SFMono-Regular", "Menlo", "Monaco", "Consolas", "monospace"],
      },
      colors: {
        // Risk-tier palette: greens are safe, ambers are warning, reds are critical.
        risk: {
          50: "#f0fdf4",
          critical: "#b91c1c",
          high: "#c2410c",
          medium: "#b45309",
          low: "#15803d",
          info: "#1d4ed8",
        },
      },
    },
  },
  plugins: [],
};

export default config;
