from html.parser import HTMLParser
from pathlib import Path

UI_ROOT = Path(__file__).resolve().parents[1] / "ui"


def ui_styles() -> str:
    """Read the stylesheet entry point and its split source files."""
    paths = [UI_ROOT / "styles.css", *sorted((UI_ROOT / "styles").glob("*.css"))]
    return "\n".join(path.read_text(encoding="utf-8") for path in paths)


class DialogAuditParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.dialogs: list[dict[str, object]] = []
        self._current: dict[str, object] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "dialog":
            self._current = {
                "id": values.get("id"),
                "has_title": False,
                "has_accessible_close": False,
            }
            self.dialogs.append(self._current)
        elif self._current is not None and tag == "h2":
            self._current["has_title"] = True
        elif self._current is not None and tag == "button":
            classes = (values.get("class") or "").split()
            if "icon-button" in classes and values.get("data-close-dialog"):
                self._current["has_accessible_close"] = bool(
                    values.get("aria-label") or values.get("title")
                )

    def handle_endtag(self, tag: str) -> None:
        if tag == "dialog":
            self._current = None


def test_dialogs_have_visible_titles_and_accessible_close_controls() -> None:
    parser = DialogAuditParser()
    parser.feed((UI_ROOT / "index.html").read_text(encoding="utf-8"))
    assert parser.dialogs
    assert all(dialog["id"] for dialog in parser.dialogs)
    assert all(dialog["has_title"] for dialog in parser.dialogs)
    assert all(dialog["has_accessible_close"] for dialog in parser.dialogs)


def test_ui_declares_responsive_and_reduced_motion_boundaries() -> None:
    html = (UI_ROOT / "index.html").read_text(encoding="utf-8")
    css = ui_styles()
    assert 'name="viewport"' in html
    assert ":focus-visible" in css
    assert "prefers-reduced-motion: reduce" in css
    assert "@media (max-width: 560px)" in css


def test_dynamic_dialog_names_and_stewardship_live_regions_are_initialized() -> None:
    script = (UI_ROOT / "app.js").read_text(encoding="utf-8")
    assert "function prepareAccessibility()" in script
    assert 'dialog.setAttribute("aria-labelledby", title.id)' in script
    assert 'setAttribute("aria-live", "polite")' in script
    assert "prepareAccessibility();" in script


def test_ui_entrypoint_loads_split_runtime_assets() -> None:
    html = (UI_ROOT / "index.html").read_text(encoding="utf-8")
    assets = (
        "core.js",
        "api.js",
        "virtual-table.js",
        "features/integration-policy.js",
        "features/transformation-workbench.js",
        "features/context-lineage-control-plane.js",
        "features/product-ai-control-plane.js",
        "features/control-center.js",
    )
    for name in assets:
        assert f'/scripts/{name}' in html
        assert (UI_ROOT / "scripts" / name).is_file()


def test_completed_control_plane_features_have_a_tabbed_ui() -> None:
    html = (UI_ROOT / "index.html").read_text(encoding="utf-8")
    script = (UI_ROOT / "scripts/features/control-center.js").read_text(encoding="utf-8")

    assert 'id="controls-view"' in html
    assert 'role="tablist"' in html
    for tab in (
        "catalog",
        "access",
        "policy",
        "reliability",
        "compliance",
        "studio",
        "plans",
        "bi",
    ):
        assert f'data-control-tab="{tab}"' in html
    assert "/v1/abac/policies" in script
    assert "/v1/observability/slo" in script
    assert "/v1/compliance/packs" in script
    assert "/v1/studio/change-sets" in script
    assert "/v1/tool-plans" in script


def test_catalog_uses_bounded_server_paging_and_lazy_view_loading() -> None:
    script = (UI_ROOT / "app.js").read_text(encoding="utf-8")
    core = (UI_ROOT / "scripts/core.js").read_text(encoding="utf-8")

    assert "catalogPageSize: 50" in core
    assert 'params.set("q", query)' in script
    assert 'params.set("object_type", type)' in script
    assert 'data-catalog-page="next"' in script
    assert "loadViewData(activeView)" in script
    organization_loader = script.split("async function loadOrganizationData()", 1)[1].split(
        "async function loadViewData", 1
    )[0]
    assert "loadControlCenter()" not in organization_loader
    assert "fetchAll(`/v1/datasources/${sourceId}/tables`)" not in script


def test_context_product_and_unified_lineage_surfaces_are_wired() -> None:
    html = (UI_ROOT / "index.html").read_text(encoding="utf-8")
    script = (UI_ROOT / "scripts/features/context-lineage-control-plane.js").read_text(
        encoding="utf-8"
    )

    assert 'id="context-products-view"' in html
    assert 'id="unified-lineage-view"' in html
    assert 'id="context-product-form"' in html
    assert "unified-lineage/impact" in script
    assert "deny_on_critical_incident" in script


def test_marketplace_compiler_and_ai_registry_surfaces_are_wired() -> None:
    html = (UI_ROOT / "index.html").read_text(encoding="utf-8")
    context_script = (UI_ROOT / "scripts/features/context-lineage-control-plane.js").read_text(
        encoding="utf-8"
    )
    platform_script = (UI_ROOT / "scripts/features/product-ai-control-plane.js").read_text(
        encoding="utf-8"
    )

    assert 'id="marketplace-view"' in html
    assert 'id="ai-registry-view"' in html
    assert 'id="context-compiler-output"' in html
    assert "context-product-versions/${versionId}/compile" in context_script
    assert "/marketplace/products" in platform_script
    assert "/ai-asset-versions/${versionId}/trust" in platform_script
