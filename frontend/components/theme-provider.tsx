"use client";

import { createContext, useContext, useEffect, useState } from "react";

type ThemeContextType = {
  isDark: boolean;
  toggleTheme: () => void;
};

const ThemeContext = createContext<ThemeContextType>({
  isDark: false,
  toggleTheme: () => {},
});

const STORAGE_KEY = "theme";

export function ThemeProvider({ children }: { children: React.ReactNode }) {
  const [isDark, setIsDark] = useState(false);

  useEffect(() => {
    const root = document.documentElement;
    const initialDark =
      root.classList.contains("dark") ||
      window.localStorage.getItem(STORAGE_KEY) === "dark" ||
      (!window.localStorage.getItem(STORAGE_KEY) &&
        window.matchMedia("(prefers-color-scheme: dark)").matches);

    if (initialDark) {
      root.classList.add("dark");
      setIsDark(true);
    } else {
      root.classList.remove("dark");
      setIsDark(false);
    }
  }, []);

  const toggleTheme = () => {
    setIsDark((prev) => {
      const next = !prev;
      const root = document.documentElement;
      if (next) {
        root.classList.add("dark");
      } else {
        root.classList.remove("dark");
      }
      try {
        window.localStorage.setItem(STORAGE_KEY, next ? "dark" : "light");
        document.cookie = `${STORAGE_KEY}=${next ? "dark" : "light"}; path=/; max-age=31536000; samesite=lax`;
      } catch {
        // Storage access may be blocked in restricted environments
      }
      return next;
    });
  };

  return (
    <ThemeContext.Provider value={{ isDark, toggleTheme }}>
      {children}
    </ThemeContext.Provider>
  );
}

export function useTheme(): ThemeContextType {
  return useContext(ThemeContext);
}
