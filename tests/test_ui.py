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

    # Zurück-Taste des Handys schließt den Betrachter, bleibt aber auf der Seite.
    page.go_back()
    expect(page.get_by_test_id("viewer")).to_be_hidden()
    assert page.url.rstrip("/").split("#")[0] == live_server.url


# --- Logs ----------------------------------------------------------------

def test_logs_are_shown_at_the_bottom(ui, live_server):
    live_server.ensure_frames("wien", 1)
    is_last = ui.evaluate(
        "() => { const s = [...document.querySelectorAll('main > section')];"
        " return s[s.length - 1].id; }"
    )
    assert is_last == "sec-logs"
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
    catalog.append({"id": "iodc", "driver": "msg_seviri", "label": "MSG Indischer Ozean",
                    "collection": "EO:EUM:DAT:MSG:HRSEVIRI-IODC", "enabled": True})
    editor.fill(json.dumps(catalog, indent=2))
    ui.get_by_test_id("sources-json-save").click()
    expect(ui.get_by_test_id("sources-json-message")).to_have_text("Quellen-Katalog gespeichert.")
    expect(ui.get_by_test_id("source-row-iodc")).to_be_visible()
    assert "iodc" in [e["id"] for e in _stored(live_server)["sources"]["catalog"]]

    # Serverseitige Validierung: Region verweist noch auf msg_seviri.
    editor.fill(json.dumps([e for e in catalog if e["id"] != "msg_seviri"]))
    ui.get_by_test_id("sources-json-save").click()
    expect(ui.get_by_test_id("sources-json-message")).to_contain_text("Speichern fehlgeschlagen")

    # Die neue Quelle steht der Region sofort zur Auswahl.
    ui.get_by_test_id("region-source-wien").select_option("iodc")
    expect(ui.get_by_test_id("refresh-message-wien")).to_have_text("Quelle gespeichert.")
    regions = {r["name"]: r for r in _stored(live_server)["regions"]}
    assert regions["wien"]["source"] == "iodc"


def _stored_catalog_or_default(page) -> list:
    return page.evaluate("fetch('/api/sources').then(r => r.json()).then(d => d.catalog)")


def test_toggle_source_and_region_warning(ui, live_server):
    expect(ui.get_by_test_id("region-warning-wien")).to_be_hidden()
    ui.get_by_test_id("source-enabled-msg_seviri").uncheck()
    expect(ui.get_by_test_id("sources-table-message")).to_have_text("Quelle msg_seviri deaktiviert.")
    expect(ui.get_by_test_id("region-warning-wien")).to_be_visible()
    catalog = {e["id"]: e for e in _stored(live_server)["sources"]["catalog"]}
    assert catalog["msg_seviri"]["enabled"] is False

    ui.get_by_test_id("source-enabled-msg_seviri").check()
    expect(ui.get_by_test_id("sources-table-message")).to_have_text("Quelle msg_seviri aktiviert.")
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
