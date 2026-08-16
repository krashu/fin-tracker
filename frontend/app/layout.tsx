import type { Metadata } from "next";
import { cookies } from "next/headers";
import { Hanken_Grotesk, JetBrains_Mono } from "next/font/google";
import { TooltipProvider } from "@/components/ui/tooltip";
import { Providers } from "@/components/providers";
import { RouteGuard } from "@/components/auth/route-guard";
import "./globals.css";

// App fonts, declared once at the root and exposed as CSS vars on <html> so
// every page (and Radix portals at <body>) inherits them. globals.css wires
// --font-hanken into --font-sans; the inline `var(--font-jbmono)` mono columns
// (expenses board, review queue, txn dialog) resolve via this same inheritance,
// so BOTH variables must stay on <html> — dropping --font-jbmono silently
// falls those columns back to ui-monospace with a green build.
const hanken = Hanken_Grotesk({
  subsets: ["latin"],
  display: "swap",
  variable: "--font-hanken",
});

const jbMono = JetBrains_Mono({
  subsets: ["latin"],
  display: "swap",
  variable: "--font-jbmono",
});

export const metadata: Metadata = {
  title: "Fin Tracker",
  description: "Personal finance tracker",
};

export default async function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  let isDark = false;
  if (process.env.NEXT_OUTPUT_MODE === "standalone") {
    try {
      const cookieStore = await cookies();
      isDark = cookieStore.get("theme")?.value === "dark";
    } catch {
      isDark = false;
    }
  }

  return (
    <html
      lang="en"
      className={`${hanken.variable} ${jbMono.variable} ${isDark ? "dark" : ""}`}
    >
      <body className="text-[13px] leading-[1.35] antialiased">
        <Providers>
          <TooltipProvider delayDuration={150}>
            <RouteGuard isDark={isDark}>{children}</RouteGuard>
          </TooltipProvider>
        </Providers>
      </body>
    </html>
  );
}
