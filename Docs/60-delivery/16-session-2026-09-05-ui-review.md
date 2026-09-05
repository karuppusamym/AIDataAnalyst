# UI review and repairs — 5 September 2026

Reviewed the application shell, screen styles, scrolling containers, responsive
layouts, and workflow wiring. This extends the workflow revalidation in report 15.

## Confirmed problems and repairs

- The shell hid overflow, leaving document-style pages (including description
  drafts, AI registry, gateway, roster and inbox) without a usable page scrollbar.
  The shell now scrolls these pages and resets the page container on navigation.
- Several form/dashboard pages combined a fixed viewport height with shrinking
  flex children. They now grow with their content. Virtualized workspaces retain
  bounded lists and a minimum usable results area when editors expand.
- Responsive rules frequently used browser width rather than the actual space
  left after navigation. Breakpoints now account for sidebar width and stack core results/detail layouts,
  preserve measurable virtual lists, and collapse narrow forms and summary tiles.
- Sidebar groups previously exposed the entire product at once. They now expand
  one workbench at a time, follow the active route, and retain full-product search
  and the current workbench's page tabs. Added an interaction regression test.
- Relationships and Reliability shared the global `.rel` CSS namespace. Renamed
  Reliability's namespace so visiting either page cannot restyle the other.
- Form labels and navigation metadata were unusually small. Increased their
  readable size; protected form widths, long text, and tile values from overflow.
- Parsed-lineage review fetched only the first 100 rows, with no next-page
  control, and included a hidden bulk-action placeholder. Added pagination,
  selection, reason-required single/bulk decisions, partial-failure feedback,
  and a keyboard-focusable horizontal table scroller. Added tests for pagination
  and bulk refusal reporting. Removed the placeholder.
- Removed obsolete tool-plan comments describing functionality as single-step
  only; the implemented editor supports multiple steps and recommendations.

## Workflow behavior retained

Evidence JSON uses authenticated requests. Metadata-derived semantic suggestions
can be reviewed and edited. Description drafts can be human-edited without
automatically publishing them. Tool plans can be authored, copied, or recommended
from prompts, then explicitly validated/executed through governed tools. Completed
analysis can become a draft tool after parameter review. Lineage supports a focused
neighborhood, fit/zoom, expansion, and a separate impact pane.

## Verification limits

Browser discovery returned no available browsers; attempting the in-app browser
also reported unavailable. Therefore this review does not claim screenshot-based
visual signoff at desktop/mobile sizes. Automated component tests validate behavior,
not computed browser geometry. A connected-browser walkthrough is still needed to
verify the final rendered result and real deployment integrations.

The workspace is receiving concurrent edits from another process. Unrelated work
was preserved. Build/type checks were rerun after transient errors during those
edits. The initial full UI run had one timing failure under heavy parallel load;
the final run uses fewer workers.

Final checks: 348 UI tests passed across 54 files; 24 targeted backend tests
passed (`test_workflow_revalidation.py` and `test_tool_plans.py`). TypeScript
checking and the production build passed. Pytest could not write its optional
cache due to filesystem permissions; test execution completed successfully.
