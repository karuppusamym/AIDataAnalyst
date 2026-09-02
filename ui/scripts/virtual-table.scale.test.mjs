// UX-3 / CT-2 (List/table virtualization at scale) proof.
//
// Exercises the three pure functions `renderRemoteTable`'s paint loop is
// built from -- `computeVisibleRange`, `pagesForRange`, `evictPages` -- at
// a synthetic 1,000,000-row scale. These are exactly what keeps
// virtual-table.js's remote/windowed mode from ever materializing more
// than a small, fixed number of rows or server pages client-side,
// regardless of how large `totalCount` is: the same "prove the pure
// windowing function's output stays bounded" approach
// graph-engine.virtualization.test.mjs (LN-8) and
// graph-engine.clustering.test.mjs (KG-3) already established for this
// codebase. No test runner exists for ui/ (a plain, un-bundled browser
// app) -- run directly via `node ui/scripts/virtual-table.scale.test.mjs`.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import vm from "node:vm";

const here = path.dirname(fileURLToPath(import.meta.url));
const source = readFileSync(path.join(here, "virtual-table.js"), "utf8");

// virtual-table.js is a plain browser IIFE reading `{ table, empty }` off
// `window.AtlasUI` at load time -- neither is invoked by the pure
// functions under test here, so bare stubs are enough.
const sandbox = { window: { AtlasUI: { table: () => "", empty: () => "" } }, console };
vm.createContext(sandbox);
vm.runInContext(source, sandbox, { filename: "virtual-table.js" });
const { computeVisibleRange, pagesForRange, evictPages, renderTable, renderRemoteTable } = sandbox.window.AtlasUI;

assert.equal(typeof computeVisibleRange, "function", "virtual-table.js must export computeVisibleRange");
assert.equal(typeof pagesForRange, "function", "virtual-table.js must export pagesForRange");
assert.equal(typeof evictPages, "function", "virtual-table.js must export evictPages");
assert.equal(typeof renderTable, "function", "existing in-memory renderTable must still be exported (no regression)");
assert.equal(typeof renderRemoteTable, "function", "virtual-table.js must export the new renderRemoteTable entry point");

const ONE_MILLION = 1_000_000;
const ROW_HEIGHT = 56;
const OVERSCAN = 4;

// --- computeVisibleRange stays bounded regardless of totalRows ---
{
  const viewportHeight = 560; // ~10 rows tall, matches virtual-table.js's own `visibleRows`
  const positions = [0, 1, ONE_MILLION * ROW_HEIGHT * 0.5, ONE_MILLION * ROW_HEIGHT - viewportHeight];
  positions.forEach(scrollTop => {
    const { firstIndex, lastIndex } = computeVisibleRange(scrollTop, viewportHeight, ROW_HEIGHT, OVERSCAN, ONE_MILLION);
    const windowSize = lastIndex - firstIndex;
    assert.ok(windowSize <= 20, `rendered window must stay small (~viewport + overscan) at 1M rows, got ${windowSize} at scrollTop=${scrollTop}`);
    assert.ok(firstIndex >= 0 && lastIndex <= ONE_MILLION, "range must stay within [0, totalRows]");
  });
}

// --- first page: no negative indices ---
{
  const { firstIndex, lastIndex } = computeVisibleRange(0, 560, ROW_HEIGHT, OVERSCAN, ONE_MILLION);
  assert.equal(firstIndex, 0, "must clamp to 0 at the very top even with overscan");
  assert.ok(lastIndex > 0);
}

// --- last page: clamps to totalRows, does not run past the end ---
{
  const scrollTop = ONE_MILLION * ROW_HEIGHT; // scrolled all the way to the bottom
  const { firstIndex, lastIndex } = computeVisibleRange(scrollTop, 560, ROW_HEIGHT, OVERSCAN, ONE_MILLION);
  assert.equal(lastIndex, ONE_MILLION, "must clamp to totalRows at the bottom");
  assert.ok(firstIndex < ONE_MILLION);
}

// --- empty list ---
{
  const { firstIndex, lastIndex } = computeVisibleRange(0, 560, ROW_HEIGHT, OVERSCAN, 0);
  assert.equal(firstIndex, 0);
  assert.equal(lastIndex, 0);
}

// --- pagesForRange: a small viewport window against a 1M-row, 200-per-page dataset needs only a handful of pages ---
{
  const { firstIndex, lastIndex } = computeVisibleRange(500_000 * ROW_HEIGHT, 560, ROW_HEIGHT, OVERSCAN, ONE_MILLION);
  const pages = pagesForRange(firstIndex, lastIndex, 200);
  assert.ok(pages.length >= 1 && pages.length <= 2, `a single small viewport window should need 1-2 pages of 200, got ${pages.length}`);
  pages.forEach(page => assert.ok(page >= 0 && page < ONE_MILLION / 200, "page indices must stay within the dataset's page count"));
}

// Array.from(...) normalizes to this realm's Array before comparing --
// values returned across the vm sandbox boundary carry that context's own
// Array.prototype, which trips deepStrictEqual's prototype check even when
// every element matches.
assert.deepEqual(Array.from(pagesForRange(0, 0, 200)), [], "an empty range needs no pages");
assert.deepEqual(Array.from(pagesForRange(199, 201, 200)), [0, 1], "a range straddling a page boundary needs both pages");

// --- evictPages: cache never grows past maxCached regardless of how many pages scroll through ---
{
  const cache = new Map();
  const maxCached = 40;
  for (let page = 0; page < 5000; page += 1) {
    cache.set(page, [`row-${page}`]);
    evictPages(cache, [page], maxCached); // simulate: viewport is currently over this one page
    assert.ok(cache.size <= maxCached, `cache must never exceed ${maxCached} entries, was ${cache.size} at page ${page}`);
  }
  assert.ok(cache.has(4999), "the most recently viewed page must survive eviction");
}

// --- evictPages: pages currently in view are never evicted, even under pressure ---
{
  const cache = new Map();
  for (let page = 0; page < 100; page += 1) cache.set(page, [`row-${page}`]);
  const keepPages = [10, 11, 12];
  evictPages(cache, keepPages, 5);
  keepPages.forEach(page => assert.ok(cache.has(page), `page ${page} is in the current viewport and must survive eviction`));
  assert.ok(cache.size <= 5);
}

// --- end-to-end simulation: scripted scroll through a synthetic 1,000,000-row remote table never grows the cache unbounded ---
{
  const heads = ["Action", "Principal", "Resource", "Outcome", "Occurred"];
  const pageSize = 200;
  let fetchCount = 0;
  const fetchPage = async (offset, limit) => {
    fetchCount += 1;
    const items = [];
    for (let i = 0; i < limit && offset + i < ONE_MILLION; i += 1) items.push({ id: offset + i });
    return { items, total: ONE_MILLION };
  };
  const rowRenderer = (item) => `<tr><td>${item.id}</td></tr>`;

  // Minimal DOM stand-in: virtual-table.js only touches a handful of DOM
  // APIs (classList, querySelector, innerHTML, style, scroll properties,
  // addEventListener) -- enough of a fake to drive the real paint/fetch
  // loop end to end without a browser.
  function makeFakeElement() {
    const children = new Map();
    const el = {
      classList: { add() {}, remove() {} },
      style: {},
      _innerHTML: "",
      attrs: {},
      listeners: {},
      scrollTop: 0,
      clientHeight: 560,
      setAttribute(name, value) { this.attrs[name] = value; },
      removeAttribute(name) { delete this.attrs[name]; },
      addEventListener(name, handler) { this.listeners[name] = handler; },
      querySelector(selector) { return children.get(selector); },
      set innerHTML(html) {
        this._innerHTML = html;
        // Register the fixed child nodes virtual-table.js looks up by class/tag.
        children.set(".virtual-count", makeLeaf());
        const scroll = makeFakeElement();
        children.set(".virtual-scroll", scroll);
        children.set(".virtual-spacer", makeFakeElement());
        const tableNode = makeFakeElement();
        const thead = makeLeaf();
        const tbody = makeLeaf();
        tableNode.querySelector = (sel) => (sel === "thead" ? thead : sel === "tbody" ? tbody : undefined);
        children.set(".virtual-table", tableNode);
      },
      get innerHTML() { return this._innerHTML; },
    };
    return el;
  }
  function makeLeaf() {
    return { _text: "", set textContent(v) { this._text = v; }, get textContent() { return this._text; }, _html: "", set innerHTML(v) { this._html = v; }, get innerHTML() { return this._html; } };
  }

  const container = makeFakeElement();
  container.innerHTML = ""; // seed children before first real render call, mirroring first mount

  renderRemoteTable(container, heads, { totalCount: ONE_MILLION, fetchPage, rowRenderer, pageSize, maxCachedPages: 10 });
  // Await the in-flight page fetches the initial paint kicked off.
  await new Promise(resolve => setTimeout(resolve, 0));
  await new Promise(resolve => setTimeout(resolve, 0));

  assert.ok(fetchCount >= 1, "mounting must fetch at least the first visible page");
  assert.ok(container._vtRemote.cache.size <= 10, "cache must respect maxCachedPages immediately after mount");

  // Simulate scrolling through the entire 1,000,000-row list in large jumps.
  const scroller = container.querySelector(".virtual-scroll");
  const positions = [50_000, 250_000, 500_000, 750_000, 999_000].map(row => row * ROW_HEIGHT);
  for (const scrollTop of positions) {
    scroller.scrollTop = scrollTop;
    scroller.listeners.scroll();
    await new Promise(resolve => setTimeout(resolve, 0));
    await new Promise(resolve => setTimeout(resolve, 0));
    assert.ok(container._vtRemote.cache.size <= 10, `cache must stay bounded at scrollTop=${scrollTop}, was ${container._vtRemote.cache.size}`);
  }

  assert.ok(fetchCount < 200, `must not have fetched anywhere close to all ${ONE_MILLION / pageSize} pages -- only ${fetchCount} fetches for a full scroll sweep`);
}

console.log("virtual-table.scale.test.mjs: all assertions passed");
