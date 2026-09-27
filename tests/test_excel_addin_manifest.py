"""The Excel add-in manifest agrees with the files it points at.

Office loads the add-in entirely from URLs in `ui-next/excel-addin/manifest.xml`.
A manifest naming a page, an icon or an origin that the build does not serve
fails inside Excel with an unhelpful error, far from the change that caused it.
This pins the manifest to the repository: every URL is HTTPS on one origin, the
pages it names are Vite entries that exist, and the icons it names are files.

It does not validate the manifest against Microsoft's schema -- that needs
`npx office-addin-manifest validate`, which downloads the schema -- and it
cannot load the add-in in Excel. See `ui-next/excel-addin/README.md`.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlparse

from defusedxml import ElementTree

REPO = Path(__file__).resolve().parents[1]
UI = REPO / "ui-next"
MANIFEST = UI / "excel-addin" / "manifest.xml"
URL_PATTERN = re.compile(r'(?:DefaultValue="|<AppDomain>)(https?://[^"<]+)')


def _urls() -> list[str]:
    return URL_PATTERN.findall(MANIFEST.read_text(encoding="utf-8"))


def test_the_manifest_is_well_formed_and_has_a_stable_id() -> None:
    root = ElementTree.parse(MANIFEST).getroot()
    ns = {"o": "http://schemas.microsoft.com/office/appforoffice/1.1"}
    identifier = root.findtext("o:Id", namespaces=ns)
    assert identifier is not None
    assert re.fullmatch(r"[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}", identifier)
    assert root.findtext("o:Permissions", namespaces=ns) == "ReadWriteDocument"


def test_every_url_is_https_on_one_origin() -> None:
    urls = _urls()
    assert urls, "the manifest names no URLs"
    origins = {f"{urlparse(url).scheme}://{urlparse(url).netloc}" for url in urls}
    assert all(url.startswith("https://") for url in urls), "Office refuses non-HTTPS pages"
    assert len(origins) == 1, f"the manifest mixes origins: {sorted(origins)}"


def test_the_pages_it_loads_are_built_by_vite() -> None:
    pages = {urlparse(url).path for url in _urls() if url.endswith(".html")}
    assert pages == {"/excel-addin.html"}
    vite_config = (UI / "vite.config.ts").read_text(encoding="utf-8")
    for page in (*pages, "/excel-addin-auth.html"):
        assert (UI / page.lstrip("/")).is_file(), page
        assert f'"{page.lstrip("/")}"' in vite_config, f"{page} is not a Vite build input"


def test_the_icons_it_names_exist() -> None:
    icons = [urlparse(url).path for url in _urls() if url.endswith(".png")]
    assert icons
    for icon in icons:
        assert (UI / "public" / icon.lstrip("/")).is_file(), icon
