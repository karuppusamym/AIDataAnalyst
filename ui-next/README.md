# ui-next — Atlas experience shell

The React rebuild of the portal, per
`Docs/10-architecture/adr/ADR-0021-experience-shell-stack-and-strangle-migration.md`.

**The strangle migration is finished.** This is the only portal: the vanilla-JS `ui/`
app was deleted on 2026-09-05 (finding D05 of `../Docs/review-2026-09-05/REVIEW.md`,
resolved by outright removal rather than a parity gate), along with its compose service
and nginx route. There is no `legacy` nav marker and no second frontend to fall back to.

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
Vite hot-module reload; changes under `src/` restart the API automatically. The
overlay uses polling so HMR also works reliably with Windows bind mounts.
Use `docker compose up --build -d` (without the overlay) to return to the
production-like setup; the built React portal is then served by nginx at
<http://localhost:3001>. Nothing runs on port 3000 any more.

The production-like image builds the React app and puts nginx in front of it.
`ui-next/nginx.conf` proxies **both** API path prefixes to the API container on
the same origin — `/v1/` and `/mcp` — and everything else falls through to the SPA.
That pair has to stay equal to `vite.config.ts`'s `server.proxy` keys, because the
Agent gateway screen tells an engineer the MCP endpoint is `${location.origin}/mcp`;
`scripts/check_proxy_contract.py` fails CI if the two configurations diverge (F07).

The image uses live API calls by default (`compose.yaml` passes
`UI_NEXT_USE_FIXTURES:-0`). Set `UI_NEXT_USE_FIXTURES=1` before
`docker compose up --build` if a fixture-backed image is needed for local UI work.

## Authentication modes (F06)

Two independent build-time axes, neither inferred from the other:

| Variable | Values | Meaning |
| --- | --- | --- |
| `VITE_USE_FIXTURES` | `0` / anything else | live backend, or bundled demo data. **Unset, the build mode decides**: `npm run dev` and `vite build --mode demo` carry demo data, every other build (`npm run build` included) does not |
| `VITE_AUTH_MODE` | `development` (default) / `oidc` / `proxy` | how a request proves who is making it |

Demo data is excluded from a production build rather than merely switched off
in it (R11-X1). `src/lib/fixtures.ts` is ~189 kB minified — it used to be about
a fifth of the shipped JavaScript, downloaded by every production user to serve
a code path they could never take, because the demo/live test was read at
runtime and no bundler can drop a branch it only learns about in the browser.
`vite.config.ts` now settles the question while configuring the build (see
`src/lib/demoDataMode.ts`) and hands the client a literal, so Rollup folds the
guard in `src/lib/api/transport.ts` and drops the fixtures with it. A demo
build keeps them as their own lazily fetched chunk rather than preloading them
with the shell.

The consequence to know about: flipping between demo and live now means
rebuilding. A production build has no fixtures in it to fall back to.

`development` sends `X-Principal-Id`/`X-Roles`. `proxy` sends nothing — an
authenticating reverse proxy is the authority. `oidc` sends only
`Authorization: Bearer`, and additionally needs

| Variable | Example |
| --- | --- |
| `VITE_OIDC_ISSUER` | `http://localhost:8090/atlas` |
| `VITE_OIDC_CLIENT_ID` | `atlas-ui-next` |
| `VITE_OIDC_SCOPE` | `openid profile email` (default) |
| `VITE_OIDC_REDIRECT_PATH` | `/` (default) |

Endpoint URLs are **not** configured: `src/lib/oidcClient.ts` reads the
issuer's `.well-known/openid-configuration` at sign-in time. The flow is
authorization code with PKCE (`S256`, `crypto.subtle`, no dependency). The
access and refresh tokens are held **in memory only** — a reload signs you out,
which is deliberate: a token in web storage is readable by any injected script
and outlives the tab. Only the PKCE verifier, `state` and `nonce` go to
`sessionStorage`, because they have to survive the redirect; they are
single-use and removed the moment the callback is handled.

An `oidc` build with no issuer configured is blocked with an explanation
instead of a sign-in button that could not work.

To run this end to end locally, use the OIDC compose overlay described in the
root `README.md`. It starts a mock issuer, which proves the protocol
integration and nothing about a corporate IdP's directory, MFA or revocation.

`npm run dev` on the host defaults to fixtures, so the Catalog runs without a
backend: it generates a 1,000,000-row catalog lazily and mirrors the server's
keyset cursor contract.

```bash
VITE_USE_FIXTURES=0 npm run dev    # proxies /v1 and /mcp to the API on :8000
```

```bash
npm run build      # tsc -b && vite build -> dist/, served by the existing nginx
npm run typecheck
```

## Layout

```
src/
  tokens.css              colour, type, spacing; both themes; focus; reduced-motion
  App.tsx                 shell, persona, nav, routing
  lib/types.ts            mirrors src/aida/schemas.py — hand-written, see UX-14
  lib/api.ts              one fetch wrapper; typed errors; every request abortable
  lib/fixtures.ts         1M-row catalog computed per index, never materialised
  components/             primitives, CatalogTable, EvidencePane,
                          ProposalCard, PropagationLog
  screens/                one component per entry in lib/routes.ts's SCREEN_IDS
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
