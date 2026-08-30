/* Large result sets stay responsive without changing the table markup contract. */
(function initializeVirtualTable() {
  const { table, empty } = window.AtlasUI;
  const rowHeight = 56;
  const threshold = 150;
  const visibleRows = 10;
  const overscan = 4;

  function renderTable(target, heads, rows, emptyText="No records found") {
    const container = typeof target === "string" ? document.getElementById(target) : target;
    if (!container) return;
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
    const firstIndex = Math.max(0, Math.floor(scroller.scrollTop / rowHeight) - overscan);
    const visibleCount = Math.ceil((scroller.clientHeight || visibleRows * rowHeight) / rowHeight) + overscan * 2;
    const lastIndex = Math.min(rows.length, firstIndex + visibleCount);
    tableNode.querySelector("thead").innerHTML = `<tr>${heads.map(head => `<th>${head}</th>`).join("")}</tr>`;
    tableNode.querySelector("tbody").innerHTML = rows.slice(firstIndex, lastIndex).join("");
    tableNode.style.transform = `translateY(${firstIndex * rowHeight}px)`;
    const countNode = container.querySelector(".virtual-count");
    if (countNode) countNode.textContent = `Showing ${rows.length ? firstIndex + 1 : 0}-${lastIndex} of ${rows.length} rows; virtualized for smooth large-estate browsing`;
  }

  Object.assign(window.AtlasUI, { renderTable });
})();
