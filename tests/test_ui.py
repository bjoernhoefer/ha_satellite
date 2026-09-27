"""Klicktests für die Web-UI (Playwright, echter Browser).

Diese Tests sichern die Grundfunktionen der Konfigurationsseite ab, damit
Layout- oder Styling-Änderungen sie nicht unbemerkt brechen. Selektoren
laufen ausschließlich über ``data-testid``-Attribute - das Markup darf sich
also frei ändern, solange diese IDs erhalten bleiben.
"""

from __future__ import annotations

import json
import os
import re

import pytest
import yaml

# Lokal ohne Playwright überspringen; in CI (HA_SATELLITE_REQUIRE_UI_TESTS=1)
# muss ein fehlendes Playwright hart fehlschlagen statt still zu skippen.
if os.environ.get("HA_SATELLITE_REQUIRE_UI_TESTS"):
    import playwright.sync_api as playwright_api
else:
    playwright_api = pytest.importorskip("playwright.sync_api")
expect = playwright_api.expect

pytestmark = pytest.mark.ui


@pytest.fixture
def ui(page, live_server):
    """Öffnet die UI und schlägt fehl, sobald JS-Fehler auftreten."""
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.goto(live_server.url + "/")
    yield page
    assert errors == [], f"JavaScript-Fehler in der UI: {errors}"


def _stored_credentials(server) -> dict:
    return yaml.safe_load(server.config_file.read_text(encoding="utf-8"))["eumetsat"]


def test_page_shows_all_core_sections(ui):
    for testid in (
        "credentials-form",
        "consumer-key",
        "consumer-secret",
        "save-credentials",
        "region-wien",
        "region-mallorca",
        "refresh-wien",
        "sources-form",
        "poll-interval",
        "auto-sync-hours",
        "sources-table",
        "sources-sync",
        "sources-json-details",
        "storage-path",
        "save-storage",
        "logs",
        "live-wien",
        "play-wien",
        "region-source-wien",
        "region-composite-wien",
        "region-borders-wien",
        "api-region",
        "api-link-latest",
    ):
        expect(ui.get_by_test_id(testid)).to_be_visible()
    expect(ui.get_by_test_id("consumer-key")).to_be_editable()
    expect(ui.get_by_test_id("consumer-secret")).to_be_editable()
    expect(ui.get_by_test_id("consumer-secret")).to_have_attribute("type", "password")


def test_enter_and_save_credentials(ui, live_server):
    ui.get_by_test_id("consumer-key").fill("my-consumer-key")
    ui.get_by_test_id("consumer-secret").fill("my-consumer-secret-ABCD")
    ui.get_by_test_id("save-credentials").click()

    expect(ui.get_by_test_id("credentials-message")).to_have_text("Zugangsdaten gespeichert.")
    expect(ui.get_by_test_id("secret-masked")).to_have_text(re.compile(r"^\*+ABCD$"))
    expect(ui.get_by_test_id("consumer-secret")).to_have_value("")
    assert _stored_credentials(live_server) == {
        "consumer_key": "my-consumer-key",
        "consumer_secret": "my-consumer-secret-ABCD",
    }

    # Nach dem Neuladen: Key sichtbar, Secret nur maskiert, nie im Klartext.
    ui.reload()
    expect(ui.get_by_test_id("consumer-key")).to_have_value("my-consumer-key")
    expect(ui.get_by_test_id("secret-masked")).to_have_text(re.compile(r"^\*+ABCD$"))
    expect(ui.get_by_test_id("consumer-secret")).to_have_value("")
    assert "my-consumer-secret" not in ui.content()


def test_saving_without_new_secret_keeps_existing_secret(ui, live_server):
    ui.get_by_test_id("consumer-key").fill("key-1")
    ui.get_by_test_id("consumer-secret").fill("first-secret-1111")
    ui.get_by_test_id("save-credentials").click()
    expect(ui.get_by_test_id("credentials-message")).to_have_text("Zugangsdaten gespeichert.")

    ui.reload()
    ui.get_by_test_id("consumer-key").fill("key-2")
    ui.get_by_test_id("save-credentials").click()
    expect(ui.get_by_test_id("credentials-message")).to_have_text("Zugangsdaten gespeichert.")

    assert _stored_credentials(live_server) == {
        "consumer_key": "key-2",
        "consumer_secret": "first-secret-1111",
    }


def test_env_override_locks_fields(page, start_server):
    server = start_server(
        env={"EUMETSAT_CONSUMER_KEY": "env-key", "EUMETSAT_CONSUMER_SECRET": "env-secret-7777"}
    )
    page.goto(server.url + "/")
    expect(page.get_by_test_id("env-override-notice")).to_be_visible()
    expect(page.get_by_test_id("consumer-key")).to_be_disabled()
    expect(page.get_by_test_id("consumer-secret")).to_be_disabled()
    expect(page.get_by_test_id("secret-masked")).to_have_text(re.compile(r"7777$"))


def test_change_poll_interval(ui, live_server):
    ui.get_by_test_id("poll-interval").fill("5")
    ui.get_by_test_id("save-sources").click()
    expect(ui.get_by_test_id("sources-message")).to_have_text("Einstellungen gespeichert.")

    ui.reload()
    expect(ui.get_by_test_id("poll-interval")).to_have_value("5")
    stored = yaml.safe_load(live_server.config_file.read_text(encoding="utf-8"))
    assert stored["sources"]["poll_interval_minutes"] == 5


def test_refresh_button_stays_on_page_and_renders_frame(ui, live_server):
    ui.get_by_test_id("refresh-mallorca").click()
    expect(ui.get_by_test_id("refresh-message-mallorca")).to_have_text("Aktualisierung angestoßen.")
    assert ui.url.rstrip("/") == live_server.url

    # Der Scheduler rendert im Hintergrund; danach muss die Vorschau laden.
    def preview_loaded() -> bool:
        ui.reload()
        return ui.get_by_test_id("preview-mallorca").evaluate(
            "img => img.complete && img.naturalWidth > 0"
        )

    for _ in range(50):
        if preview_loaded():
            break
        ui.wait_for_timeout(200)
    else:
        pytest.fail("Vorschaubild für mallorca wurde nicht geladen")


# --- Hilfen -------------------------------------------------------------

def _viewport_filled(page, testid: str = "viewer-image") -> None:
    """Das Bild muss den kompletten Viewport einnehmen (kein kleines Popup)."""
    box = page.get_by_test_id(testid).bounding_box()
    size = page.viewport_size
    assert box is not None
    assert abs(box["x"]) < 1 and abs(box["y"]) < 1
    assert abs(box["width"] - size["width"]) < 2
    assert abs(box["height"] - size["height"]) < 2


def _image_loaded(page, testid: str) -> None:
    page.wait_for_function(
        "id => { const i = document.querySelector(`[data-testid=\"${id}\"]`);"
        " return i && i.complete && i.naturalWidth > 0; }",
        arg=testid,
    )


def _stored(server) -> dict:
    return yaml.safe_load(server.config_file.read_text(encoding="utf-8"))


# --- Betrachter / Historie / Live ------------------------------------------

def test_preview_click_opens_fullscreen_viewer_with_history(ui, live_server):
    frames = live_server.ensure_frames("wien", 3)
    ui.reload()
    ui.get_by_test_id("preview-wien").click()

    viewer = ui.get_by_test_id("viewer")
    expect(viewer).to_be_visible()
    _image_loaded(ui, "viewer-image")
    _viewport_filled(ui)
    total = len(live_server.frames("wien"))
    assert total >= len(frames)
    expect(ui.get_by_test_id("viewer-position")).to_have_text(f"{total} / {total}")
    expect(ui.get_by_test_id("viewer-caption")).to_contain_text("neuestes Bild")
    expect(ui.get_by_test_id("viewer-next")).to_be_disabled()

    ui.get_by_test_id("viewer-prev").click()
    expect(ui.get_by_test_id("viewer-position")).to_have_text(f"{total - 1} / {total}")
    assert "view=wien" in ui.url and "i=1" in ui.url
    ui.keyboard.press("ArrowRight")
    expect(ui.get_by_test_id("viewer-position")).to_have_text(f"{total} / {total}")

    ui.keyboard.press("Escape")
    expect(viewer).to_be_hidden()
    assert "#" not in ui.url or ui.url.endswith("#")


def test_history_thumbnail_opens_that_frame(ui, live_server):
    live_server.ensure_frames("wien", 3)
    ui.reload()
    item = ui.get_by_test_id("history-item-wien-2")
    expect(item).to_be_visible()
    frames = live_server.frames("wien")
    item.click()
    expect(ui.get_by_test_id("viewer")).to_be_visible()
    expect(ui.get_by_test_id("viewer-position")).to_have_text(f"{len(frames) - 2} / {len(frames)}")
    src = ui.get_by_test_id("viewer-image").get_attribute("src")
    assert src.endswith(frames[2]["filename"])
    ui.get_by_test_id("viewer-close").click()
    expect(ui.get_by_test_id("viewer")).to_be_hidden()


def _zoom(page) -> float:
    return float(page.get_by_test_id("viewer").get_attribute("data-zoom") or 1)


def test_viewer_frame_dropdown_selects_frame(ui, live_server):
    live_server.ensure_frames("wien", 3)
    ui.reload()
    ui.get_by_test_id("preview-wien").click()
    expect(ui.get_by_test_id("viewer")).to_be_visible()
    frames = live_server.frames("wien")
    total = len(frames)
    select = ui.get_by_test_id("viewer-frame")
    expect(select).to_be_visible()
    expect(select.locator("option")).to_have_count(total)
    expect(select).to_have_value("0")
    expect(select.locator("option").first).to_contain_text("neuestes")

    select.select_option("2")
    expect(ui.get_by_test_id("viewer-position")).to_have_text(f"{total - 2} / {total}")
    assert ui.get_by_test_id("viewer-image").get_attribute("src").endswith(frames[2]["filename"])
    assert "i=2" in ui.url

    # Blättern hält die Auswahlliste synchron.
    ui.get_by_test_id("viewer-next").click()
    expect(select).to_have_value("1")

    ui.get_by_test_id("viewer-mode").click()  # Live: keine Frame-Auswahl
    expect(select).to_be_hidden()


def test_viewer_zoom_buttons_keys_and_pan(ui, live_server):
    live_server.ensure_frames("wien", 2)
    ui.reload()
    ui.get_by_test_id("preview-wien").click()
    expect(ui.get_by_test_id("viewer")).to_be_visible()
    _image_loaded(ui, "viewer-image")
    expect(ui.get_by_test_id("viewer-zoom-reset")).to_have_text("100 %")
    expect(ui.get_by_test_id("viewer-zoom-out")).to_be_disabled()

    ui.get_by_test_id("viewer-zoom-in").click()
    expect(ui.get_by_test_id("viewer-zoom-reset")).to_have_text("150 %")
    ui.get_by_test_id("viewer-zoom-in").click()
    ui.get_by_test_id("viewer-zoom-in").click()
    expect(ui.get_by_test_id("viewer-zoom-reset")).to_have_text("300 %")
    box = ui.get_by_test_id("viewer-image").bounding_box()
    assert box["width"] > ui.viewport_size["width"] * 2.5

    # Ziehen mit der Maus verschiebt das gezoomte Bild, ohne die Leisten auszublenden.
    before = ui.get_by_test_id("viewer-image").evaluate("i => i.style.transform")
    vw, vh = ui.viewport_size["width"], ui.viewport_size["height"]
    ui.mouse.move(vw / 2, vh / 2)
    ui.mouse.down()
    ui.mouse.move(vw / 2 + 120, vh / 2 + 80, steps=5)
    ui.mouse.up()
    after = ui.get_by_test_id("viewer-image").evaluate("i => i.style.transform")
    assert after != before
    expect(ui.get_by_test_id("viewer")).not_to_have_class(re.compile("controls-hidden"))

    # Zoom bleibt beim Blättern erhalten.
    ui.get_by_test_id("viewer-prev").click()
    assert _zoom(ui) == 3

    ui.keyboard.press("-")
    expect(ui.get_by_test_id("viewer-zoom-reset")).to_have_text("200 %")
    ui.keyboard.press("0")
    expect(ui.get_by_test_id("viewer-zoom-reset")).to_have_text("100 %")
    _viewport_filled(ui)
    ui.keyboard.press("+")
    assert _zoom(ui) == 1.5
    ui.get_by_test_id("viewer-zoom-reset").click()
    assert _zoom(ui) == 1

    # Schließen setzt den Zoom zurück.
    ui.get_by_test_id("viewer-zoom-in").click()
    ui.keyboard.press("Escape")
    ui.get_by_test_id("preview-wien").click()
    expect(ui.get_by_test_id("viewer-zoom-reset")).to_have_text("100 %")


def test_timelapse_plays_through_history(ui, live_server):
    live_server.ensure_frames("wien", 3)
    ui.reload()
    ui.get_by_test_id("play-wien").click()
    expect(ui.get_by_test_id("viewer")).to_be_visible()
    expect(ui.get_by_test_id("viewer-play")).to_have_text("❚❚")
    seen = set()
    for _ in range(30):
        seen.add(ui.get_by_test_id("viewer-position").text_content())
        if len(seen) >= 3:
            break
        ui.wait_for_timeout(150)
    assert len(seen) >= 3, seen
    ui.keyboard.press(" ")
    expect(ui.get_by_test_id("viewer-play")).to_have_text("▶")


def test_live_stream_in_browser(ui, live_server):
    live_server.ensure_frames("wien", 1)
    ui.reload()
    ui.get_by_test_id("live-wien").click()
    expect(ui.get_by_test_id("viewer")).to_be_visible()
    expect(ui.get_by_test_id("viewer-image")).to_have_attribute("src", re.compile(r"/regions/wien/mjpeg"))
    _image_loaded(ui, "viewer-image")
    _viewport_filled(ui)
    expect(ui.get_by_test_id("viewer-prev")).to_be_hidden()
    assert "live=wien" in ui.url

    ui.get_by_test_id("viewer-mode").click()  # Wechsel zur Historie
    expect(ui.get_by_test_id("viewer-prev")).to_be_visible()
    expect(ui.get_by_test_id("viewer-image")).to_have_attribute("src", re.compile(r"/history/"))
    ui.get_by_test_id("viewer-close").click()
    expect(ui.get_by_test_id("viewer")).to_be_hidden()
    assert ui.get_by_test_id("viewer-image").get_attribute("src") is None


def test_live_deep_link_opens_stream(page, live_server):
    live_server.ensure_frames("mallorca", 1)
    page.goto(live_server.url + "/live/mallorca")
    expect(page.get_by_test_id("viewer")).to_be_visible()
    expect(page.get_by_test_id("viewer-image")).to_have_attribute("src", re.compile(r"/regions/mallorca/mjpeg"))
    _image_loaded(page, "viewer-image")


@pytest.fixture
def mobile_page(browser, live_server):
    context = browser.new_context(
        viewport={"width": 390, "height": 844}, device_scale_factor=3, is_mobile=True, has_touch=True
    )
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    yield page
    context.close()
    assert errors == [], f"JavaScript-Fehler in der UI: {errors}"


def test_mobile_viewer_fills_screen_and_back_closes(mobile_page, live_server):
    live_server.ensure_frames("wien", 2)
    page = mobile_page
    page.goto(live_server.url + "/")
    # Kein horizontales Scrollen auf dem Handy.
    page.wait_for_load_state("load")
    page.wait_for_timeout(500)  # per JS nachgeladene Inhalte
    width = page.evaluate("document.documentElement.scrollWidth")
    offenders = page.evaluate(
        "() => [...document.querySelectorAll('body *')].filter(e => {"
        " const r = e.getBoundingClientRect(); if (r.right <= 391) return false;"
        " for (let p = e.parentElement; p; p = p.parentElement) {"
        "   const o = getComputedStyle(p).overflowX; if (o === 'auto' || o === 'hidden') return false; }"
        " return true; }).slice(0, 5).map(e => e.outerHTML.slice(0, 120))"
    )
    assert width <= 390, offenders

    page.get_by_test_id("preview-wien").tap()
    expect(page.get_by_test_id("viewer")).to_be_visible()
    _image_loaded(page, "viewer-image")
    _viewport_filled(page)
    expect(page.get_by_test_id("viewer-prev")).to_be_visible()
    expect(page.get_by_test_id("viewer-close")).to_be_in_viewport()

    page.get_by_test_id("viewer-prev").tap()
    expect(page.get_by_test_id("viewer-position")).to_contain_text("/")

    # Frame-Auswahl und Zoom sind am Handy erreichbar und bedienbar.
    for testid in ("viewer-frame", "viewer-zoom-in", "viewer-zoom-out", "viewer-zoom-reset"):
        expect(page.get_by_test_id(testid)).to_be_in_viewport()
    page.get_by_test_id("viewer-frame").select_option("0")
    expect(page.get_by_test_id("viewer-caption")).to_contain_text("neuestes Bild")
    page.get_by_test_id("viewer-zoom-in").tap()
    expect(page.get_by_test_id("viewer-zoom-reset")).to_have_text("150 %")
    page.get_by_test_id("viewer-zoom-reset").tap()
    expect(page.get_by_test_id("viewer-zoom-reset")).to_have_text("100 %")
    assert page.evaluate("document.documentElement.scrollWidth") <= 390

    # Zurück-Taste des Handys schließt den Betrachter, bleibt aber auf der Seite.
    page.go_back()
    expect(page.get_by_test_id("viewer")).to_be_hidden()
    assert page.url.rstrip("/").split("#")[0] == live_server.url


# --- Logs ----------------------------------------------------------------

def test_logs_are_shown_at_the_bottom(ui, live_server):
    live_server.ensure_frames("wien", 1)
    last_two = ui.evaluate(
        "() => [...document.querySelectorAll('main > section')].slice(-2).map((s) => s.id)"
    )
    # Logs stehen unten, nur noch gefolgt von der API-Link-Sammlung.
    assert last_two == ["sec-logs", "sec-api"]
    logs = ui.get_by_test_id("logs")
    expect(logs).to_contain_text("Render wien fertig", timeout=10000)

    ui.get_by_test_id("refresh-mallorca").click()
    expect(logs).to_contain_text("Manuelle Aktualisierung für mallorca", timeout=10000)

    ui.get_by_test_id("logs-level").select_option("ERROR")
    expect(ui.locator('[data-testid="log-line"][data-level="INFO"]').first).to_be_hidden()
    ui.get_by_test_id("logs-clear").click()
    expect(ui.locator('[data-testid="log-line"]')).to_have_count(0)


# --- Quellen -------------------------------------------------------------

def test_sources_json_editor_is_collapsed_and_editable(ui, live_server):
    editor = ui.get_by_test_id("sources-json")
    expect(editor).to_be_hidden()
    expect(ui.get_by_test_id("sources-json-details")).not_to_have_attribute("open", "")

    ui.get_by_test_id("sources-json-details").locator("summary").click()
    expect(editor).to_be_visible()
    expect(editor).to_have_value(re.compile(r'"msg_seviri"'))

    editor.fill("[kaputt")
    ui.get_by_test_id("sources-json-save").click()
    expect(ui.get_by_test_id("sources-json-message")).to_contain_text("Ungültiges JSON")

    catalog = _stored_catalog_or_default(ui)
    # Treiber dummy: die Region soll danach keinen echten Download auslösen.
    catalog.append({"id": "iodc", "driver": "dummy", "label": "MSG Indischer Ozean",
                    "collection": "EO:EUM:DAT:MSG:HRSEVIRI-IODC", "enabled": True})
    editor.fill(json.dumps(catalog, indent=2))
    ui.get_by_test_id("sources-json-save").click()
    expect(ui.get_by_test_id("sources-json-message")).to_have_text("Quellen-Katalog gespeichert.")
    expect(ui.get_by_test_id("source-row-iodc")).to_be_visible()
    assert "iodc" in [e["id"] for e in _stored(live_server)["sources"]["catalog"]]

    # Serverseitige Validierung: Regionen verweisen noch auf dummy.
    editor.fill(json.dumps([e for e in catalog if e["id"] != "dummy"]))
    ui.get_by_test_id("sources-json-save").click()
    expect(ui.get_by_test_id("sources-json-message")).to_contain_text("Speichern fehlgeschlagen")

    # Die neue Quelle steht der Region sofort zur Auswahl.
    ui.get_by_test_id("region-source-wien").select_option("iodc")
    expect(ui.get_by_test_id("refresh-message-wien")).to_contain_text("Quelle gespeichert")
    regions = {r["name"]: r for r in _stored(live_server)["regions"]}
    assert regions["wien"]["source"] == "iodc"


def _stored_catalog_or_default(page) -> list:
    return page.evaluate("fetch('/api/sources').then(r => r.json()).then(d => d.catalog)")


def test_toggle_source_and_region_warning(ui, live_server):
    expect(ui.get_by_test_id("region-warning-wien")).to_be_hidden()
    ui.get_by_test_id("source-enabled-dummy").uncheck()
    expect(ui.get_by_test_id("sources-table-message")).to_have_text("Quelle dummy deaktiviert.")
    expect(ui.get_by_test_id("region-warning-wien")).to_be_visible()
    catalog = {e["id"]: e for e in _stored(live_server)["sources"]["catalog"]}
    assert catalog["dummy"]["enabled"] is False

    ui.get_by_test_id("source-enabled-dummy").check()
    expect(ui.get_by_test_id("sources-table-message")).to_have_text("Quelle dummy aktiviert.")
    expect(ui.get_by_test_id("region-warning-wien")).to_be_hidden()


def test_sync_with_eumetsat_and_adopt(page, start_server, fake_eumetsat):
    server = start_server(env={"HA_SATELLITE_EUMETSAT_API": fake_eumetsat})
    page.goto(server.url + "/")
    expect(page.get_by_test_id("sources-last-sync")).to_have_text("noch nie")
    page.get_by_test_id("sources-sync").click()
    expect(page.get_by_test_id("sources-sync-message")).to_have_text("Abgleich abgeschlossen.")
    expect(page.get_by_test_id("source-sync-status-msg_seviri")).to_contain_text("verfügbar")
    expect(page.get_by_test_id("discovered-EO-EUM-DAT-MSG-HRSEVIRI-IODC")).to_be_visible()

    page.get_by_test_id("adopt-EO-EUM-DAT-MSG-HRSEVIRI-IODC").click()
    expect(page.get_by_test_id("sources-sync-message")).to_contain_text("übernommen")
    expect(page.get_by_test_id("discovered-EO-EUM-DAT-MSG-HRSEVIRI-IODC")).to_have_count(0)
    catalog = {e["collection"]: e for e in _stored(server)["sources"]["catalog"]}
    assert catalog["EO:EUM:DAT:MSG:HRSEVIRI-IODC"]["enabled"] is False


def test_sync_failure_is_shown(ui):
    ui.get_by_test_id("sources-sync").click()
    expect(ui.get_by_test_id("sources-sync-message")).to_contain_text("Abgleich fehlgeschlagen")


# --- Speicherort ---------------------------------------------------------

def test_change_storage_location_via_ui(page, live_server):
    live_server.ensure_frames("wien", 2)
    (live_server.storage_root / "data").mkdir()
    target = live_server.storage_root / "data" / "ha_satellite"
    page.goto(live_server.url + "/")
    expect(page.get_by_test_id("storage-current")).to_have_text(str(live_server.data_dir / "frames"))

    candidate = page.locator(f'[data-testid^="storage-candidate-"][data-path="{target}"]')
    expect(candidate).to_be_visible()
    candidate.click()
    expect(page.get_by_test_id("storage-path")).to_have_value(str(target))
    page.get_by_test_id("history-minutes").fill("180")
    page.get_by_test_id("save-storage").click()

    expect(page.get_by_test_id("storage-message")).to_contain_text(f"Speicherort: {target}")
    expect(page.get_by_test_id("storage-current")).to_have_text(str(target))
    assert list((target / "wien").glob("*.png"))
    stored = _stored(live_server)
    assert stored["storage"]["frames_dir"] == str(target)
    assert stored["history"]["history_minutes"] == 180
    _image_loaded(page, "preview-wien")
    expect(page.get_by_test_id("history-item-wien-0")).to_be_visible()


def test_invalid_storage_location_shows_error(ui):
    ui.get_by_test_id("storage-path").fill("relativer/pfad")
    ui.get_by_test_id("save-storage").click()
    expect(ui.get_by_test_id("storage-message")).to_contain_text("absolut")


# --- Quelle/Bildtyp wechseln --------------------------------------------------

def test_change_composite_rerenders_and_updates_preview(ui, live_server):
    live_server.ensure_frames("wien", 1)
    before = {f["filename"] for f in live_server.frames("wien")}
    select = ui.get_by_test_id("region-composite-wien")
    expect(select).to_have_value("natural_color_hrv_with_night_ir")

    select.select_option("natural_color_hrv")
    expect(ui.get_by_test_id("refresh-message-wien")).to_contain_text("Bildtyp gespeichert")
    regions = {r["name"]: r for r in _stored(live_server)["regions"]}
    assert regions["wien"]["composite"] == "natural_color_hrv"

    # Ohne weiteren Klick: der Server rendert neu, die Historie lädt nach.
    expect(ui.get_by_test_id("history-item-wien-0")).to_be_visible()
    ui.wait_for_function(
        "(n) => document.querySelectorAll('[data-testid^=\"history-item-wien-\"]').length > n",
        arg=len(before), timeout=15000,
    )
    latest = live_server.frames("wien")[0]
    assert latest["filename"] not in before
    assert latest["composite"] == "natural_color_hrv"


def test_toggle_borders_rerenders(ui, live_server):
    live_server.ensure_frames("wien", 1)
    before = {f["filename"] for f in live_server.frames("wien")}
    box = ui.get_by_test_id("region-borders-wien")
    expect(box).to_be_checked()

    box.uncheck()
    expect(ui.get_by_test_id("refresh-message-wien")).to_contain_text("Landesgrenzen ausgeschaltet")
    regions = {r["name"]: r for r in _stored(live_server)["regions"]}
    assert regions["wien"]["borders"] is False
    assert regions["mallorca"]["borders"] is True

    ui.wait_for_function(
        "(n) => document.querySelectorAll('[data-testid^=\"history-item-wien-\"]').length > n",
        arg=len(before), timeout=15000,
    )
    latest = live_server.frames("wien")[0]
    assert latest["filename"] not in before
    assert latest["borders"] is False

    ui.reload()
    expect(ui.get_by_test_id("region-borders-wien")).not_to_be_checked()


def test_borders_checkbox_on_mobile(mobile_page, live_server):
    mobile_page.goto(live_server.url + "/")
    box = mobile_page.get_by_test_id("region-borders-wien")
    expect(box).to_be_visible()
    box.tap()
    expect(mobile_page.get_by_test_id("refresh-message-wien")).to_contain_text("Landesgrenzen ausgeschaltet")


def test_placeholder_source_is_marked(ui, live_server):
    expect(ui.get_by_test_id("region-placeholder-wien")).to_be_visible()  # Test-Server: dummy
    options = ui.get_by_test_id("region-source-wien").locator("option")
    expect(options.filter(has_text="Data Tailor")).to_contain_text("Platzhalter")
    expect(options.filter(has_text="Rapid Scan")).not_to_contain_text("Platzhalter")
    expect(ui.get_by_test_id("source-row-data_tailor")).to_contain_text("Platzhalter")

    ui.get_by_test_id("region-source-wien").select_option("msg_seviri")
    expect(ui.get_by_test_id("refresh-message-wien")).to_contain_text("Quelle gespeichert")
    expect(ui.get_by_test_id("region-placeholder-wien")).to_be_hidden()


# --- API-Links ----------------------------------------------------------------

def test_api_links_follow_selected_region_and_work(ui, live_server):
    live_server.ensure_frames("mallorca", 1)
    latest = ui.get_by_test_id("api-link-latest")
    expect(latest).to_have_attribute("href", "/regions/wien/latest.png")
    expect(latest).to_have_text(live_server.url + "/regions/wien/latest.png")

    ui.get_by_test_id("api-region").select_option("mallorca")
    expect(latest).to_have_attribute("href", "/regions/mallorca/latest.png")
    expect(ui.get_by_test_id("api-link-mjpeg")).to_have_attribute("href", "/regions/mallorca/mjpeg")
    expect(ui.get_by_test_id("api-link-live")).to_have_attribute("href", "/live/mallorca")
    expect(ui.get_by_test_id("api-post-1")).to_contain_text("/api/regions/mallorca/refresh")
    # Allgemeine Links hängen nicht vom Standort ab.
    expect(ui.get_by_test_id("api-link-status")).to_have_attribute("href", "/api/status")

    # Anklicken öffnet einen neuen Tab, die UI bleibt stehen.
    with ui.context.expect_page() as new_tab:
        latest.click()
    tab = new_tab.value
    tab.wait_for_load_state()
    assert tab.url == live_server.url + "/regions/mallorca/latest.png"
    assert tab.evaluate("() => document.images[0] && document.images[0].naturalWidth") > 0
    tab.close()
    assert ui.url.startswith(live_server.url + "/")

    # Alle lesenden Links antworten (außer MJPEG/MP4: Stream bzw. optional).
    for link in ui.locator("[data-testid^='api-link-']").all():
        testid = link.get_attribute("data-testid")
        if testid in ("api-link-mjpeg", "api-link-mp4"):
            continue
        href = link.get_attribute("href")
        response = ui.request.get(live_server.url + href, max_redirects=0)
        assert response.status in (200, 307), (href, response.status)


def test_api_links_on_mobile_do_not_overflow(mobile_page, live_server):
    mobile_page.goto(live_server.url + "/#sec-api")
    expect(mobile_page.get_by_test_id("api-link-latest")).to_be_visible()
    width = mobile_page.evaluate("() => document.documentElement.scrollWidth")
    assert width <= 390


# --- MTG FCI: Bildtypen, Rohdaten-Archiv ----------------------------------------

def _png(color) -> bytes:
    import io

    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (40, 40), color).save(out, format="PNG")
    return out.getvalue()


def test_switching_to_fci_offers_fci_composites(ui, live_server):
    select = ui.get_by_test_id("region-composite-wien")
    expect(select).to_have_value("natural_color_hrv_with_night_ir")

    ui.get_by_test_id("region-source-wien").select_option("mtg_fci")
    expect(ui.get_by_test_id("refresh-message-wien")).to_contain_text("Quelle und Bildtyp gespeichert")
    expect(select).to_have_value("natural_color_with_night_cloudtop")
    expect(select.locator("option", has_text="HRV")).to_have_count(0)
    region = {r["name"]: r for r in _stored(live_server)["regions"]}["wien"]
    assert (region["source"], region["composite"]) == ("mtg_fci", "natural_color_with_night_cloudtop")
    expect(ui.get_by_test_id("region-placeholder-wien")).to_be_hidden()

    # "natural_color" gibt es für beide Satelliten -> bleibt beim Zurückwechseln.
    select.select_option("natural_color")
    expect(ui.get_by_test_id("refresh-message-wien")).to_contain_text("Bildtyp gespeichert")
    ui.get_by_test_id("region-source-wien").select_option("msg_seviri")
    expect(ui.get_by_test_id("refresh-message-wien")).to_contain_text("Quelle gespeichert")
    expect(select).to_have_value("natural_color")
    expect(select.locator("option", has_text="HRV").first).to_be_attached()


def test_archive_viewer_browses_slots_and_composites(ui, live_server):
    from test_fci import write_fci_slot

    frames = live_server.data_dir / "frames"
    write_fci_slot(frames, "20260926T141000Z", cached={("wien", "natural_color_with_night_cloudtop"): _png("red")})
    write_fci_slot(frames, "20260926T142000Z", cached={
        ("wien", "natural_color_with_night_cloudtop"): _png("green"),
        ("wien", "cloudtop"): _png("blue"),
    })
    ui.get_by_test_id("archive-wien").click()
    viewer = ui.get_by_test_id("viewer")
    expect(viewer).to_be_visible()
    expect(ui.get_by_test_id("viewer-position")).to_have_text("2 / 2")
    expect(ui.get_by_test_id("viewer-image")).to_have_attribute("src", re.compile(r"/archive/20260926T142000Z\.png"))
    _image_loaded(ui, "viewer-image")
    expect(ui.get_by_test_id("viewer-caption")).to_contain_text("MTG FCI")
    expect(ui.get_by_test_id("viewer-play")).to_be_hidden()
    expect(ui.get_by_test_id("viewer-loading")).to_be_hidden()
    assert "archive=wien" in ui.url

    ui.get_by_test_id("viewer-prev").click()
    expect(ui.get_by_test_id("viewer-position")).to_have_text("1 / 2")
    expect(ui.get_by_test_id("viewer-image")).to_have_attribute("src", re.compile(r"20260926T141000Z"))

    ui.get_by_test_id("viewer-next").click()
    ui.get_by_test_id("viewer-composite").select_option("cloudtop")
    expect(ui.get_by_test_id("viewer-image")).to_have_attribute("src", re.compile(r"142000Z\.png\?composite=cloudtop"))
    _image_loaded(ui, "viewer-image")
    assert "c=cloudtop" in ui.url

    ui.get_by_test_id("viewer-mode").click()  # zurück zur normalen Historie
    expect(ui.get_by_test_id("viewer-composite")).to_be_hidden()
    ui.get_by_test_id("viewer-close").click()
    expect(viewer).to_be_hidden()


def test_archive_viewer_without_data_explains_why(ui):
    ui.get_by_test_id("archive-mallorca").click()
    expect(ui.get_by_test_id("viewer")).to_contain_text("Keine FCI-Rohdaten im Archiv")


def test_archive_settings_are_saved(ui, live_server):
    expect(ui.get_by_test_id("archive-summary")).to_contain_text("Keine MTG-FCI-Quelle aktiv")
    expect(ui.get_by_test_id("archive-summary")).to_contain_text("32–40")
    ui.get_by_test_id("archive-retention").fill("6")
    ui.get_by_test_id("archive-chunk-min").fill("30")
    ui.get_by_test_id("save-storage").click()
    expect(ui.get_by_test_id("storage-message")).to_contain_text("Gespeichert")
    assert _stored(live_server)["archive"] == {"retention_hours": 6, "chunk_min": 30, "chunk_max": 40}
    expect(ui.get_by_test_id("archive-summary")).to_contain_text("30–40")
