# ui-next — Atlas experience shell

The React rebuild of the portal, per
`Docs/10-architecture/adr/ADR-0021-experience-shell-stack-and-strangle-migration.md`.

`ui/` still serves every screen not yet migrated. This app owns routing, navigation,
persona and chrome from day one; screens marked `legacy` in the nav have not moved
across yet. There is no cutover — `ui/` is deleted when the last marker goes.

## Run

```bash
npm install
npm run dev        # http://localhost:5174
```

Fixtures are on by default, so the Catalog runs without a backend: it generates a
1,000,000-row catalog lazily and mirrors the server's keyset cursor contract.

```bash
VITE_USE_FIXTURES=0 npm run dev    # proxies /v1 to the API on :8000
```

Fixtures-off needs `GET /v1/organizations/{org}/catalog/rows` (tracker UX-12), which
does not exist yet. `src/lib/types.ts` is already typed against it.

```bash
npm run build      # tsc -b && vite build -> dist/, served by the existing nginx
npm run typecheck
```

## Layout

```
src/
  tokens.css              colour, type, spacing; both themes; focus; reduced-motion
  App.tsx                 shell, persona, nav, strangle seam
  lib/types.ts            mirrors src/aida/schemas.py — hand-written, see UX-14
  lib/api.ts              one fetch wrapper; typed errors; every request abortable
  lib/fixtures.ts         1M-row catalog computed per index, never materialised
  components/             primitives, CatalogTable, EvidencePane
  screens/CatalogScreen.tsx
```

## Adding a screen

Copy the Catalog pattern — it is the deliverable, more than the screen is. The eight
rules are in `Docs/40-engineering/08-experience-shell-rebuild-plan.md` §3. The two that
get forgotten first:

- **Shareable state goes in the URL**, not component state. Filters and selection are
  query parameters, which is what makes evidence permalinkable (UX-7).
- **One abortable request in flight per view**, with a sequence guard discarding late
  responses. Racing writes are why a list shows rows that don't match the filter above it.

And the rule that matters most in this product: a model-proposed value is never
rendered as an established one (ADR-0001).
