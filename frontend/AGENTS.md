<!-- BEGIN:nextjs-agent-rules -->

# This is NOT the Next.js you know

This version has breaking changes — APIs, conventions, and file structure may all differ from your training data. Read the relevant guide in `node_modules/next/dist/docs/` (resolved from this file's directory; in monorepos the `next` package may not be visible from the repo root) before writing any code. Heed deprecation notices.

This block is written and re-added by `next dev` — verify at `node_modules/next/dist/server/lib/generate-agent-files.js`. Removing it from a diff only re-creates the uncommitted change; committing it with your work keeps the tree clean.

<!-- END:nextjs-agent-rules -->

## Frontend conventions (`frontend/`)

shadcn/ui is **owned source** in `components/ui/`, not a package — include it when auditing
CSS-utility dependents.

### Server state is TanStack Query — and invalidation is manual

**There are no `use*` query hooks.** Components call `useQuery` directly against a shared key
convention. `["dashboards"]`-prefixed invalidation is what makes writes refresh reads.

Defaults live in `components/providers.tsx`: `staleTime: 30_000`, `refetchOnWindowFocus: false`.
Nothing refetches on its own — **if a mutation doesn't invalidate, the UI serves stale data until
the stale window lapses.**

`lib/queries/invalidate.ts` is a written contract, not a helper. Its docstring names every caller
that must invoke it. Any mutation that learns or mutates a rule calls `invalidateRules` — otherwise
`/settings/rules` and the tagging-health tiles serve a stale list and rule actions can 404 on a
deleted id. **Read that docstring before adding a mutation.**

### The tsc blind spot

Pydantic schemas are **hand-mirrored** as TS types in `lib/api/client.ts`. Drift is only half-caught:

- tsc **does** flag a TS field the backend dropped or renamed — a call site reads it.
- tsc **cannot** see a backend field the TS type never declared. No diagnostic, nothing renders it,
  suite stays green.

So a response schema gaining a field must be **diffed by hand**. Two dashboard fields were computed
and silently discarded this way.

The gate is thinner than it looks. `eslint.config.mjs` declares zero rules (its header explains the
TypeScript 7 blocker), so `pnpm lint` passes vacuously. Vitest (`pnpm test`) covers helpers, components
and some pages, not API-shape drift — so `tsc --noEmit` is the only check on the mirror, and it has
the blind spot above.

### Components

Dashboards and forms are **Client Components** (`"use client"`) — they need interactivity and
TanStack Query. Pages that are pure layout stay Server Components.

### Money display is masked by default

`Sensitive` (from `components/balance-visibility.tsx`) hides every amount behind `••••` for
over-the-shoulder privacy. That file's header docstring is the spec:

- Client Context + localStorage, **not** a cookie + `router.refresh()` like the theme toggle. The
  mask must be instant.
- **Defaults to hidden**, so a reload never flashes real amounts.
- `Sensitive` is a text swap and **can't wrap chart internals**. Charts mask via `useBalanceHidden()`
  plus `pointer-events-none select-none blur-sm` — `pointer-events-none` is load-bearing, since
  `ChartTooltipContent` renders raw values unless passed a `formatter`.

Any new surface that renders a rupee amount goes through one of those two paths.

### API client

All calls go through `request()` in `lib/api/client.ts`. Tokens live in httpOnly cookies, so every
call must send credentials, and **a 401 triggers one silent refresh + replay inside `request()`** —
never at a call site. Bypassing it means an expired-but-refreshable session logs the user out
instead of recovering.

### Charts

`lib/charts.ts` owns period → x-axis labelling and delegates money formatting to `lib/format.ts`
(`compactINR`) and windows to `lib/dates.ts`. **Don't format money inline in a chart component.**
