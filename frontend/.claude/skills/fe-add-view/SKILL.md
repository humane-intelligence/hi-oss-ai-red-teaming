---
name: fe-add-view
description: Use when adding a new backend domain to the console as a read view (list, optionally detail). Follows the established feature-folder pattern so every domain looks and behaves the same.
---

# Add a domain view

Every domain view is the same shape. Copy an existing feature (`src/features/evaluations/`
for list+detail+search, `src/features/ai-models/` for list+detail without search,
`src/features/users/` for list-only) and adapt. Don't invent a new structure.

## Preconditions

- The typed client is current (`make gen` if the contract changed — see [fe-sync-backend](../fe-sync-backend/SKILL.md)).
- You know the endpoint paths and the response schema name from the OpenAPI spec.

## Steps

1. **Type aliases** — add the response types to `src/lib/api/types.ts`:
   ```ts
   export type WidgetResponse = components['schemas']['WidgetResponse']
   ```

2. **Queries** — `src/features/<domain>/queries.ts`. One hook per endpoint. List hooks take
   `{ limit, offset, search? }`, use `keepPreviousData`, and call through `unwrap()`:
   ```ts
   export function useWidgets(params: { limit: number; offset: number }) {
     return useQuery({
       queryKey: ['widgets', params],
       queryFn: async () => unwrap(await apiClient.GET('/api/v1/widgets', { params: { query: params } })),
       placeholderData: keepPreviousData,
     })
   }
   ```
   Detail hooks set `enabled: id !== ''` and pass `params: { path: { widget_id: id } }`.

3. **List page** — `src/features/<domain>/<domain>-list-page.tsx`. Define a `Column<T>[]`, then
   compose `PageHeader` + `DataTable` + `Pagination` (all from `@/components/shared/`). Pass the
   query's `isPending`/`isError`/`error` straight into `DataTable`. `PAGE_SIZE = 20`. Make rows
   clickable (`onRowClick` → navigate to the detail route) only if a detail page exists.

4. **Detail page** (optional) — `<domain>-detail-page.tsx`. Back button, loading/error guards,
   then `Card` + `dl` of `Field` (`@/components/shared/field`). Render JSON blobs in a
   `<pre className="… bg-muted …">`.

5. **Status badge** (if the domain has a status enum) — `<domain>/status-badge.tsx` mapping each
   enum value to a `Badge` variant. Mirror `src/features/evaluations/status-badge.tsx`.

6. **Wire it up:**
   - Add a nav entry to `src/app/layout/app-shell.tsx` (`nav` array, pick a `lucide-react` icon).
   - Add routes under the guarded `AppShell` children in `src/app/router.tsx`.

7. **Verify:** `make format && make lint` (`lint` chains `tsc`). Then `make dev` and click through the
   new view. Format first — `git push` is gated on `prettier --check` (a prek pre-push hook, and again
   in CI), so an unformatted new view is rejected before it reaches review.

## Rules

- Reuse `DataTable`, `Pagination`, `PageHeader`, `Field` — never hand-roll a table or pagination.
- Server state belongs in a query hook, never in component state.
- Errors surface automatically as a toast (global `QueryCache` handler) and inline in `DataTable`;
  don't add per-call error toasts.
- Match the column/label style of the existing features so the console stays consistent.
