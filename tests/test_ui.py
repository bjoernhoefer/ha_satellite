"""Klicktests für die Web-UI (Playwright, echter Browser).

Diese Tests sichern die Grundfunktionen der Konfigurationsseite ab, damit
Layout- oder Styling-Änderungen sie nicht unbemerkt brechen. Selektoren
laufen ausschließlich über ``data-testid``-Attribute - das Markup darf sich
also frei ändern, solange diese IDs erhalten bleiben.
"""

from __future__ import annotations

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
    expect(ui.get_by_test_id("sources-message")).to_have_text("Abrufintervall gespeichert.")

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
