import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterAll, afterEach, beforeAll, vi } from "vitest";
import { server } from "./mocks/server";

// Start mock service worker before all tests
beforeAll(() => {
  server.listen({ onUnhandledRequest: "error" });
});

import { setAuthFailureHandler } from "@/lib/api/client";

// Automatically cleanup rendered components, reset auth failure handler, and reset MSW handlers after each test
afterEach(() => {
  server.resetHandlers();
  const clearAuth = setAuthFailureHandler(() => {});
  clearAuth();
  if (typeof window !== "undefined") {
    cleanup();
  }
});

// Close mock service worker after all tests
afterAll(() => {
  server.close();
});

// Polyfill window.matchMedia for responsive components and hooks
if (typeof window !== "undefined") {
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    value: vi.fn().mockImplementation((query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addListener: vi.fn(), // Deprecated
      removeListener: vi.fn(), // Deprecated
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })),
  });

  // Polyfill ResizeObserver
  class ResizeObserverMock {
    observe = vi.fn();
    unobserve = vi.fn();
    disconnect = vi.fn();
  }
  window.ResizeObserver = ResizeObserverMock as unknown as typeof ResizeObserver;

  // Polyfill IntersectionObserver
  class IntersectionObserverMock {
    observe = vi.fn();
    unobserve = vi.fn();
    disconnect = vi.fn();
  }
  window.IntersectionObserver = IntersectionObserverMock as unknown as typeof IntersectionObserver;

  // Polyfill window.scrollTo
  window.scrollTo = vi.fn();
}
