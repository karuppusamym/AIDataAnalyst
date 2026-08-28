from html.parser import HTMLParser
from pathlib import Path

UI_ROOT = Path(__file__).resolve().parents[1] / "ui"


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
    css = (UI_ROOT / "styles.css").read_text(encoding="utf-8")
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
