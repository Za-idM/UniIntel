import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}", "./lib/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // Dark header/nav shell.
        ink: {
          900: "#0b0d12",
          800: "#12151c",
          700: "#1a1e27",
          600: "#262b36",
          500: "#3a4150",
          400: "#5b6474",
        },
        // Warm off-white content area -- not pure white.
        paper: {
          DEFAULT: "#faf8f4",
          100: "#f4f1ea",
          200: "#eae5da",
          300: "#d9d2c2",
        },
        // Industrial amber accent -- primary actions, brand mark.
        accent: {
          DEFAULT: "#c98a2c",
          50: "#fbf1de",
          100: "#f3ddac",
          400: "#d9a441",
          600: "#c98a2c",
          700: "#a06e20",
        },
        // Functional match/status colors -- independent of the accent.
        status: {
          exact: "#3f8a5c",
          exactBg: "#e8f2ea",
          close: "#c98a2c",
          closeBg: "#faf0dc",
          miss: "#b8503f",
          missBg: "#f7e9e6",
          neutral: "#8b8578",
          neutralBg: "#eeece5",
        },
        ink900: "#171a21",
      },
      fontFamily: {
        sans: ["var(--font-plex-sans)", "ui-sans-serif", "system-ui", "sans-serif"],
        mono: ["var(--font-plex-mono)", "ui-monospace", "SFMono-Regular", "monospace"],
      },
      fontSize: {
        xs: ["0.75rem", { lineHeight: "1.1rem" }],
        sm: ["0.8125rem", { lineHeight: "1.2rem" }],
      },
      borderColor: {
        DEFAULT: "#eae5da",
      },
    },
  },
  plugins: [],
};

export default config;
