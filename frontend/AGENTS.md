<!-- BEGIN:nextjs-agent-rules -->

# This is NOT the Next.js you know

This version has breaking changes — APIs, conventions, and file structure may all differ from your training data. Read the relevant guide in `node_modules/next/dist/docs/` (resolved from this file's directory; in monorepos the `next` package may not be visible from the repo root) before writing any code. Heed deprecation notices.

This block is written and re-added by `next dev` — verify at `node_modules/next/dist/server/lib/generate-agent-files.js`. Removing it from a diff only re-creates the uncommitted change; committing it with your work keeps the tree clean.

<!-- END:nextjs-agent-rules -->

## Frontend Conventions (`frontend/`)

Conventions for `frontend/`. Universal invariants and root boundaries apply.

### Local Commands

| Task | Command | Notes |
|---|---|---|
| Lint | `pnpm lint` | ESLint |
| Typecheck | `pnpm typecheck` | `tsc --noEmit` — only static check on API types |
| Test suite | `pnpm test` | Vitest |
| Single test | `pnpm vitest run <path>` | Targeted component or helper test |
| Build | `pnpm build` | Production Next.js build |

### Components & Structure

- `components/ui/` is owned source (shadcn/ui), not a third-party package.
- Interactive dashboards and forms must be Client Components (`"use client"`). Pages that are pure layout stay Server Components.

### State & Manual Invalidation

- **TanStack Query:** Components call `useQuery` directly with shared key conventions; there are no `use*` query hooks. Config defaults in `components/providers.tsx`: `staleTime: 30_000`, `refetchOnWindowFocus: false`.
- **Manual Invalidation:** Data does not refetch automatically. Mutations must call the dedicated invalidation contract functions in `lib/queries/invalidate.ts` (e.g. `invalidateRules`).

### API Client & Hand-Mirrored Types

- **Type Drift Blind Spot:** Backend Pydantic schemas are hand-mirrored as TypeScript types in `lib/api/client.ts`. While `tsc` flags dropped backend fields, it cannot detect newly added backend fields absent in TS definitions. Diff response schemas manually.
- **Request Wrapper:** All HTTP calls must use `request()` in `lib/api/client.ts`. Tokens reside in httpOnly cookies; `request()` handles credentials and automatic 401 refresh + replay.

### Privacy & Balance Masking

- Rupee amounts default to masked (`••••`) for privacy via `<Sensitive>` (`components/balance-visibility.tsx`).
- Charts cannot wrap elements in `<Sensitive>`; mask charts via `useBalanceHidden()` combined with `pointer-events-none select-none blur-sm`.
- Number formatting delegates to `lib/charts.ts`, `lib/format.ts` (`compactINR`), and `lib/dates.ts`. Never format currency inline in chart components.
