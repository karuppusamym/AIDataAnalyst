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

## Docker live development

From the repository root, start the platform with the development overlay:

```powershell
docker compose -f compose.yaml -f compose.dev.yaml up --build -d
```

Open <http://localhost:5174>. Changes under `ui-next/` are applied through
Vite hot-module reload; changes under `src/` restart the API automatically.
The full legacy portal stays available at <http://localhost:3000>; edits under
`ui/` are served straight from the bind mount after a browser refresh. The
overlay uses polling so HMR also works reliably with Windows bind mounts.
Use `docker compose up --build -d` (without the overlay) to return to the
production-like setup.

Fixtures are on by default, so the Catalog runs without a backend: it generates a
1,000,000-row catalog lazily and mirrors the server's keyset cursor contract.

```bash
VITE_USE_FIXTURES=0 npm run dev    # proxies /v1 to the API on :8000
```

Fixtures-off works against `GET /v1/organizations/{org}/catalog/rows` (tracker UX-12).
The remaining cutover work is migrating the screens that still live under `ui/`.

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
  components/             primitives, CatalogTable, EvidencePane,
                          ProposalCard, PropagationLog
  screens/                CatalogScreen, ReviewQueueScreen
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

## Two primitives that carry rules, not styles

`ProposalCard` — the unit of governed change. `rationale` and `evidence` are **required
fields on the type**, not optional props. A proposal that shows its outcome but not its
reasoning is not reviewable; the reviewer is being asked to rubber-stamp. Confidence is
rendered as a number as well as a bar, because a steward tuning an auto-apply threshold
needs the number.

`PropagationLog` — every hop names the mechanism that carried it ("via column lineage —
orders_raw.amount derives from raw_sales.amount"). ADR-0016 has quality fail closed, so a
tool call can be refused because of a check three hops upstream. "Affected" is a claim;
"affected via column lineage from raw_sales" is an argument.

Both came out of `Docs/Atlan-context.docx`; the reasoning is in
`Docs/40-engineering/08-experience-shell-rebuild-plan.md` §8.
