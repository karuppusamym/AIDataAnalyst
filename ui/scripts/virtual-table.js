/* Large result sets stay responsive without changing the table markup contract. */
(function initializeVirtualTable() {
  const { table, empty } = window.AtlasUI;
  const rowHeight = 56;
  const threshold = 150;
  const visibleRows = 10;
  const overscan = 4;

  /* UX-3 / CT-2: scale mode for lists too large to ever hold fully in
   * memory client-side (up to 1,000,000+ rows). `renderTable` below stays
   * exactly as it was -- callers that already fetch a bounded array (via
   * `fetchAll`) keep working unmodified. `renderRemoteTable` is the new,
   * separate entry point: it never materializes more than a small, bounded
   * number of rows client-side, fetching pages lazily as the viewport
   * scrolls and evicting pages that scroll back out of range, so memory
   * stays flat regardless of how large `totalCount` is. */
  const DEFAULT_PAGE_SIZE = 200;
  const DEFAULT_MAX_CACHED_PAGES = 40; // 40 * 200 = 8,000 rows resident at once, worst case

  function renderTable(target, heads, rows, emptyText="No records found") {
    const container = typeof target === "string" ? document.getElementById(target) : target;
    if (!container) return;
    container._vtRemote = null; // switching a container back to in-memory mode drops any remote session
    if (!rows.length || rows.length <= threshold) {
      container.classList.remove("virtual-table-host");
      container._vtBound = false;
      container.innerHTML = rows.length ? table(heads, rows, emptyText) : empty(emptyText);
      return;
    }
    mountVirtualTable(container, heads, rows);
  }

  function mountVirtualTable(container, heads, rows) {
    container.classList.add("virtual-table-host");
    container._vtHeads = heads;
    container._vtRows = rows;
    const viewportHeight = Math.min(rows.length, visibleRows) * rowHeight;
    if (!container._vtBound) {
      container.innerHTML = `<div class="virtual-count" role="status" aria-live="polite"></div><div class="virtual-scroll"><div class="virtual-spacer"><table class="data-table virtual-table"><thead></thead><tbody></tbody></table></div></div>`;
      container.querySelector(".virtual-scroll").addEventListener("scroll", () => paintVirtualTable(container));
      container._vtBound = true;
    }
    const scroller = container.querySelector(".virtual-scroll");
    scroller.removeAttribute("role");
    scroller.removeAttribute("aria-rowcount");
    scroller.style.height = `${viewportHeight}px`;
    scroller.scrollTop = 0;
    paintVirtualTable(container);
  }

  function paintVirtualTable(container) {
    const heads = container._vtHeads;
    const rows = container._vtRows;
    const scroller = container.querySelector(".virtual-scroll");
    const spacer = container.querySelector(".virtual-spacer");
    const tableNode = container.querySelector(".virtual-table");
    spacer.style.height = `${rows.length * rowHeight}px`;
    const { firstIndex, lastIndex } = computeVisibleRange(scroller.scrollTop, scroller.clientHeight, rowHeight, overscan, rows.length);
    tableNode.querySelector("thead").innerHTML = `<tr>${heads.map(head => `<th>${head}</th>`).join("")}</tr>`;
    tableNode.querySelector("tbody").innerHTML = rows.slice(firstIndex, lastIndex).join("");
    tableNode.style.transform = `translateY(${firstIndex * rowHeight}px)`;
    const countNode = container.querySelector(".virtual-count");
    if (countNode) countNode.textContent = `Showing ${rows.length ? firstIndex + 1 : 0}-${lastIndex} of ${rows.length} rows; virtualized for smooth large-estate browsing`;
  }

  /* ---- Pure helpers (unit-tested directly in virtual-table.scale.test.mjs, no DOM) ---- */

  /* Given scroll position, viewport size, and row height, returns the
   * [firstIndex, lastIndex) row window that needs to be in the DOM, plus a
   * fixed overscan margin either side. The returned window size depends
   * only on the viewport, never on totalRows -- this is what keeps the
   * rendered DOM node count bounded at any scale, from 150 rows to 1M. */
  function computeVisibleRange(scrollTop, viewportHeight, rowHeightPx, overscanRows, totalRows) {
    if (!totalRows) return { firstIndex: 0, lastIndex: 0 };
    const firstIndex = Math.max(0, Math.floor(scrollTop / rowHeightPx) - overscanRows);
    const visibleCount = Math.ceil((viewportHeight || rowHeightPx) / rowHeightPx) + overscanRows * 2;
    const lastIndex = Math.min(totalRows, firstIndex + visibleCount);
    return { firstIndex, lastIndex };
  }

  /* Which fixed-size server pages cover a given row range. */
  function pagesForRange(firstIndex, lastIndex, pageSize) {
    if (lastIndex <= firstIndex) return [];
    const firstPage = Math.floor(firstIndex / pageSize);
    const lastPage = Math.floor((lastIndex - 1) / pageSize);
    const pages = [];
    for (let page = firstPage; page <= lastPage; page += 1) pages.push(page);
    return pages;
  }

  /* Bounds a page cache to `maxCached` entries, always keeping every page
   * in `keepPages` (the pages currently in or near the viewport) and
   * evicting the least-recently-inserted others first. This is what keeps
   * memory flat as a user scrolls through a million-row list instead of
   * growing without bound. */
  function evictPages(cache, keepPages, maxCached) {
    if (cache.size <= maxCached) return;
    const keep = new Set(keepPages);
    for (const key of cache.keys()) {
      if (cache.size <= maxCached) break;
      if (!keep.has(key)) cache.delete(key);
    }
  }

  /* ---- Remote/windowed rendering ---- */

  function renderRemoteTable(target, heads, options) {
    const container = typeof target === "string" ? document.getElementById(target) : target;
    if (!container) return;
    const {
      totalCount,
      fetchPage,
      rowRenderer,
      pageSize = DEFAULT_PAGE_SIZE,
      maxCachedPages = DEFAULT_MAX_CACHED_PAGES,
      emptyText = "No records found",
    } = options;
    container._vtBound = false; // switching a container back to remote mode drops any in-memory session
    if (!totalCount) {
      container.classList.remove("virtual-table-host");
      container._vtRemote = null;
      container.innerHTML = empty(emptyText);
      return;
    }
    const previous = container._vtRemote;
    const sameSession = previous && previous.fetchPage === fetchPage && previous.totalCount === totalCount;
    container.classList.add("virtual-table-host");
    container._vtRemote = {
      totalCount, fetchPage, rowRenderer, pageSize, maxCachedPages, heads,
      cache: sameSession ? previous.cache : new Map(),
      pending: sameSession ? previous.pending : new Set(),
      requestToken: (previous?.requestToken || 0) + 1,
    };
    const viewportHeight = Math.min(totalCount, visibleRows) * rowHeight;
    if (!container._vtRemoteBound) {
      container.innerHTML = `<div class="virtual-count" role="status" aria-live="polite"></div><div class="virtual-scroll" role="grid" aria-label="Virtualized results"><div class="virtual-spacer"><table class="data-table virtual-table"><thead></thead><tbody></tbody></table></div></div>`;
      container.querySelector(".virtual-scroll").addEventListener("scroll", () => paintRemoteTable(container));
      container._vtRemoteBound = true;
    }
    const scroller = container.querySelector(".virtual-scroll");
    scroller.setAttribute("aria-rowcount", String(totalCount));
    scroller.style.height = `${viewportHeight}px`;
    if (!sameSession) scroller.scrollTop = 0;
    paintRemoteTable(container);
  }

  function paintRemoteTable(container) {
    const remote = container._vtRemote;
    if (!remote) return;
    const scroller = container.querySelector(".virtual-scroll");
    const spacer = container.querySelector(".virtual-spacer");
    const tableNode = container.querySelector(".virtual-table");
    spacer.style.height = `${remote.totalCount * rowHeight}px`;
    const { firstIndex, lastIndex } = computeVisibleRange(scroller.scrollTop, scroller.clientHeight, rowHeight, overscan, remote.totalCount);
    const pages = pagesForRange(firstIndex, lastIndex, remote.pageSize);
    evictPages(remote.cache, pages, remote.maxCachedPages);
    tableNode.querySelector("thead").innerHTML = `<tr>${remote.heads.map(head => `<th>${head}</th>`).join("")}</tr>`;
    const bodyRows = [];
    for (let index = firstIndex; index < lastIndex; index += 1) {
      const pageIndex = Math.floor(index / remote.pageSize);
      const page = remote.cache.get(pageIndex);
      const row = page ? page[index - pageIndex * remote.pageSize] : undefined;
      bodyRows.push(row !== undefined ? row : `<tr class="virtual-row-loading" style="height:${rowHeight}px" aria-hidden="true"><td colspan="${remote.heads.length}"></td></tr>`);
    }
    tableNode.querySelector("tbody").innerHTML = bodyRows.join("");
    tableNode.style.transform = `translateY(${firstIndex * rowHeight}px)`;
    const countNode = container.querySelector(".virtual-count");
    if (countNode) countNode.textContent = `Showing ${remote.totalCount ? firstIndex + 1 : 0}-${lastIndex} of ${remote.totalCount.toLocaleString()} rows; virtualized for smooth large-estate browsing`;
    fetchMissingPages(container, remote, pages);
  }

  function fetchMissingPages(container, remote, pages) {
    const token = remote.requestToken;
    pages.forEach(pageIndex => {
      if (remote.cache.has(pageIndex) || remote.pending.has(pageIndex)) return;
      remote.pending.add(pageIndex);
      Promise.resolve(remote.fetchPage(pageIndex * remote.pageSize, remote.pageSize))
        .then(page => {
          remote.pending.delete(pageIndex);
          if (container._vtRemote !== remote || remote.requestToken !== token) return; // stale response, superseded session
          const rows = (page.items || []).map(item => remote.rowRenderer(item));
          remote.cache.set(pageIndex, rows);
          paintRemoteTable(container);
        })
        .catch(() => { remote.pending.delete(pageIndex); });
    });
  }

  Object.assign(window.AtlasUI, {
    renderTable, renderRemoteTable,
    computeVisibleRange, pagesForRange, evictPages,
  });
})();
