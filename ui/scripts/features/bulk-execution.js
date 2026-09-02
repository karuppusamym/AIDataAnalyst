/* UX-4: bulk selection (up to 10,000 items) with background execution,
 * progress reporting, and cancellation.
 *
 * 2026-09-01's own investigation of this row (see the tracker) found no
 * async bulk-operation backend to wire to: CT-1's four bulk-* endpoints
 * (`catalog_bulk_actions.py`, mounted under
 * `/v1/organizations/{organization_id}/tables/bulk-{tag,classify,own,certify}`)
 * execute synchronously, one HTTP request/response, capped at
 * `CATALOG_BULK_ACTION_MAX_ITEMS` (500) items -- not this row's 10,000, and
 * with no in-flight status to poll or cancel. Adding real async execution
 * (a worker, a persisted processed-of-total counter, a cancellation flag
 * checked mid-run) needs new persisted state and a migration, both out of
 * ui/'s reach (src/aida/ is owned by other groups this wave).
 *
 * This module honestly delivers what a purely client-side orchestrator
 * over the existing *synchronous* endpoints can: a selection of up to
 * 10,000 items is split into chunks at the server's real 500-item cap,
 * chunks run one at a time through a non-blocking async loop (the UI stays
 * responsive throughout -- this is the "background" a single-page app can
 * offer without a server-side job), progress is reported after every
 * chunk, and cancellation is real: checked before each chunk is issued, so
 * a cancelled run stops dispatching further chunks (any chunk already
 * in flight still completes and its results are honestly counted, exactly
 * like CT-1's own partial-success contract for a single request).
 */
(function initializeBulkExecution() {
  const { api } = window.AtlasUI;

  const DEFAULT_CHUNK_SIZE = 500; // CATALOG_BULK_ACTION_MAX_ITEMS
  const MAX_SELECTION = 10000;

  /* ---- Pure helpers (unit-tested directly, no DOM/network) ---- */

  function chunkIds(ids, chunkSize = DEFAULT_CHUNK_SIZE) {
    const chunks = [];
    for (let index = 0; index < ids.length; index += chunkSize) {
      chunks.push(ids.slice(index, index + chunkSize));
    }
    return chunks;
  }

  function createProgress(total, chunkCount) {
    return {
      total, processed: 0, succeeded: 0, failed: 0,
      chunksTotal: chunkCount, chunksDone: 0,
      cancelled: false, done: false, errors: [],
    };
  }

  /* ---- Selection state (bounded at MAX_SELECTION) ---- */

  function createSelectionState(max = MAX_SELECTION) {
    const ids = new Set();
    return {
      max,
      has: (id) => ids.has(id),
      size: () => ids.size,
      list: () => [...ids],
      clear: () => ids.clear(),
      /* Returns { added, capped } -- capped is true if the selection was
       * already at `max` and this id could not be added. */
      add(id) {
        if (ids.has(id)) return { added: false, capped: false };
        if (ids.size >= max) return { added: false, capped: true };
        ids.add(id);
        return { added: true, capped: false };
      },
      remove: (id) => ids.delete(id),
      toggle(id) {
        if (ids.has(id)) { ids.delete(id); return { added: false, capped: false }; }
        return this.add(id);
      },
      /* Adds as many of `candidateIds` as fit under `max`; returns how many
       * were actually added and whether the cap was hit. */
      addMany(candidateIds) {
        let added = 0;
        let capped = false;
        for (const id of candidateIds) {
          if (ids.has(id)) continue;
          if (ids.size >= max) { capped = true; break; }
          ids.add(id);
          added += 1;
        }
        return { added, capped };
      },
    };
  }

  /* ---- Chunked, cancellable, progress-reporting execution over the real
   *      synchronous CT-1 endpoints ---- */

  async function runBulkOperation({ endpoint, buildBody, ids, chunkSize = DEFAULT_CHUNK_SIZE, onProgress, signal }) {
    const chunks = chunkIds(ids, chunkSize);
    const progress = createProgress(ids.length, chunks.length);
    onProgress?.({ ...progress });
    for (const chunk of chunks) {
      if (signal?.aborted) { progress.cancelled = true; break; }
      try {
        const run = await api(endpoint, { method: "POST", body: JSON.stringify(buildBody(chunk)) });
        progress.succeeded += run.succeeded_count ?? 0;
        progress.failed += run.failed_count ?? 0;
      } catch (error) {
        progress.failed += chunk.length;
        progress.errors.push(error.message || "Bulk chunk failed");
      }
      progress.processed += chunk.length;
      progress.chunksDone += 1;
      onProgress?.({ ...progress });
    }
    progress.done = true;
    onProgress?.({ ...progress });
    return progress;
  }

  Object.assign(window.AtlasUI, {
    BULK_MAX_SELECTION: MAX_SELECTION,
    BULK_DEFAULT_CHUNK_SIZE: DEFAULT_CHUNK_SIZE,
    chunkIds,
    createProgress,
    createSelectionState,
    runBulkOperation,
  });
})();
