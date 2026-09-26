"""API-Tests rund um das Speichern der EUMETSAT-Zugangsdaten."""

from __future__ import annotations

import httpx
import yaml


def _stored(server) -> dict:
    return yaml.safe_load(server.config_file.read_text(encoding="utf-8"))


def test_credentials_are_persisted_and_masked(live_server):
    response = httpx.post(
        f"{live_server.url}/api/config",
        json={"eumetsat": {"consumer_key": "my-key", "consumer_secret": "super-secret-1234"}},
    )
    assert response.status_code == 200
    assert response.json()["eumetsat"] == {
        "consumer_key": "my-key",
        "consumer_secret": "*************1234",
    }

    assert _stored(live_server)["eumetsat"]["consumer_secret"] == "super-secret-1234"
    fetched = httpx.get(f"{live_server.url}/api/config").json()
    assert "super-secret" not in fetched["eumetsat"]["consumer_secret"]


def test_empty_or_masked_secret_keeps_existing_value(live_server):
    httpx.post(
        f"{live_server.url}/api/config",
        json={"eumetsat": {"consumer_key": "k", "consumer_secret": "super-secret-1234"}},
    )
    masked = httpx.get(f"{live_server.url}/api/config").json()["eumetsat"]["consumer_secret"]

    for secret in ("", masked, None):
        response = httpx.post(
            f"{live_server.url}/api/config",
            json={"eumetsat": {"consumer_key": "k2", "consumer_secret": secret}},
        )
        assert response.status_code == 200
        assert _stored(live_server)["eumetsat"] == {
            "consumer_key": "k2",
            "consumer_secret": "super-secret-1234",
        }


def test_empty_env_vars_do_not_override_saved_credentials(start_server):
    # docker compose setzt bei ${VAR:-} eine *leere* Variable.
    server = start_server(env={"EUMETSAT_CONSUMER_KEY": "", "EUMETSAT_CONSUMER_SECRET": ""})
    httpx.post(
        f"{server.url}/api/config",
        json={"eumetsat": {"consumer_key": "ui-key", "consumer_secret": "ui-secret-9876"}},
    )
    effective = httpx.get(f"{server.url}/api/config").json()["eumetsat"]
    assert effective["consumer_key"] == "ui-key"
    assert effective["consumer_secret"].endswith("9876")


def test_env_credentials_are_never_written_to_file(start_server):
    server = start_server(
        env={"EUMETSAT_CONSUMER_KEY": "env-key", "EUMETSAT_CONSUMER_SECRET": "env-secret-5555"}
    )
    httpx.post(f"{server.url}/api/config", json={"sources": {"poll_interval_minutes": 10}})

    stored = _stored(server)
    assert stored["sources"]["poll_interval_minutes"] == 10
    assert stored["eumetsat"] == {"consumer_key": "", "consumer_secret": ""}
    effective = httpx.get(f"{server.url}/api/config").json()["eumetsat"]
    assert effective["consumer_key"] == "env-key"


def test_invalid_config_is_rejected(live_server):
    response = httpx.post(
        f"{live_server.url}/api/config", json={"sources": {"poll_interval_minutes": 0}}
    )
    assert response.status_code == 400
