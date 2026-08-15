import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { type RenderOptions, render } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import React, { type ReactElement } from "react";
import { BalanceVisibilityProvider } from "@/components/balance-visibility";

export function createTestQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
        staleTime: 0,
        gcTime: 0,
      },
      mutations: {
        retry: false,
      },
    },
  });
}

interface CustomRenderOptions extends Omit<RenderOptions, "wrapper"> {
  queryClient?: QueryClient;
  initialBalanceHidden?: boolean;
}

export function renderWithProviders(
  ui: ReactElement,
  {
    queryClient = createTestQueryClient(),
    initialBalanceHidden = true,
    ...renderOptions
  }: CustomRenderOptions = {},
) {
  if (typeof window !== "undefined") {
    window.localStorage.setItem(
      "balance-hidden",
      initialBalanceHidden ? "1" : "0",
    );
  }

  function Wrapper({ children }: { children: React.ReactNode }) {
    return (
      <QueryClientProvider client={queryClient}>
        <BalanceVisibilityProvider>{children}</BalanceVisibilityProvider>
      </QueryClientProvider>
    );
  }

  return {
    user: userEvent.setup(),
    queryClient,
    ...render(ui, { wrapper: Wrapper, ...renderOptions }),
  };
}

export * from "@testing-library/react";
export { userEvent };
