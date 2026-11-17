import type { Metadata } from "next";
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

// Inline script executed synchronously before first paint to prevent theme flash (FOUC)
const themeScript = `(function(){try{var t=localStorage.getItem("theme");if(t==="dark"||(!t&&window.matchMedia("(prefers-color-scheme: dark)").matches)){document.documentElement.classList.add("dark")}else{document.documentElement.classList.remove("dark")}}catch(e){}})()`;

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html
      lang="en"
      suppressHydrationWarning
      className={`${hanken.variable} ${jbMono.variable}`}
    >
      <head>
        <script dangerouslySetInnerHTML={{ __html: themeScript }} />
      </head>
      <body className="text-[13px] leading-[1.35] antialiased">
        <Providers>
          <TooltipProvider delayDuration={150}>
            <RouteGuard>{children}</RouteGuard>
          </TooltipProvider>
        </Providers>
      </body>
    </html>
  );
}
