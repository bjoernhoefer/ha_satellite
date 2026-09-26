"""API-Tests: Quellen-Katalog, Abgleich, Speicherort, Historie, Logs, Live."""

from __future__ import annotations

import httpx
import yaml

from conftest import FAKE_LATEST


def _stored(server) -> dict:
    return yaml.safe_load(server.config_file.read_text(encoding="utf-8"))


def test_catalog_can_be_replaced_and_is_validated(live_server):
    catalog = httpx.get(f"{live_server.url}/api/sources").json()["catalog"]
    catalog.append({"id": "iodc", "driver": "msg_seviri", "collection": "EO:EUM:DAT:MSG:HRSEVIRI-IODC"})
    response = httpx.post(f"{live_server.url}/api/config", json={"sources": {"catalog": catalog}})
    assert response.status_code == 200
    assert "iodc" in [e["id"] for e in _stored(live_server)["sources"]["catalog"]]

    # Quelle entfernen, die noch von einer Region genutzt wird -> 400.
    without_dummy = [e for e in catalog if e["id"] != "dummy"]
    response = httpx.post(f"{live_server.url}/api/config", json={"sources": {"catalog": without_dummy}})
    assert response.status_code == 400
    assert "dummy" in response.json()["detail"]

    response = httpx.post(
        f"{live_server.url}/api/config",
        json={"sources": {"catalog": [*catalog, {"id": "bad", "driver": "nope"}]}},
    )
    assert response.status_code == 400


def test_sync_reports_availability_and_discovers_collections(start_server, fake_eumetsat):
    server = start_server(env={"HA_SATELLITE_EUMETSAT_API": fake_eumetsat})
    result = httpx.post(f"{server.url}/api/sources/sync", timeout=30).json()
    assert result["error"] is None
    assert result["sources"]["msg_seviri"]["status"] == "ok"
    assert result["sources"]["msg_seviri"]["latest_product_at"] == FAKE_LATEST
    assert result["sources"]["dummy"]["status"] == "local"
    discovered = {d["collection"] for d in result["discovered"]}
    # Bekannt sind HRSEVIRI, MSG15-RSS und 0662; Cloud Mask passt zu keinem Treiber.
    assert discovered == {"EO:EUM:DAT:MSG:HRSEVIRI-IODC", "EO:EUM:DAT:0665"}

    adopted = httpx.post(
        f"{server.url}/api/sources/adopt", json={"collection": "EO:EUM:DAT:0665"}
    ).json()
    assert adopted["driver"] == "mtg_fci"
    assert adopted["enabled"] is False
    sources = httpx.get(f"{server.url}/api/sources").json()
    assert adopted["id"] in [e["id"] for e in sources["catalog"]]
    assert "EO:EUM:DAT:0665" not in {d["collection"] for d in sources["sync"]["discovered"]}
    assert (server.data_dir / "source_sync.json").exists()


def test_sync_failure_is_reported_not_raised(live_server):
    result = httpx.post(f"{live_server.url}/api/sources/sync", timeout=30).json()
    assert result["error"]


def test_disabled_source_is_not_scheduled_but_manual_refresh_works(live_server):
    catalog = httpx.get(f"{live_server.url}/api/sources").json()["catalog"]
    for entry in catalog:
        entry["enabled"] = entry["id"] == "dummy"
    regions = httpx.get(f"{live_server.url}/api/config").json()["regions"]
    for region in regions:
        region["source"] = "dummy" if region["name"] == "wien" else "msg_seviri"
    response = httpx.post(
        f"{live_server.url}/api/config", json={"sources": {"catalog": catalog}, "regions": regions}
    )
    assert response.status_code == 200
    assert live_server.ensure_frames("mallorca", 1)


def test_history_endpoints(live_server):
    frames = live_server.ensure_frames("wien", 3)
    assert [f["index"] for f in frames] == list(range(len(frames)))
    assert frames[0]["created_at"] > frames[-1]["created_at"]

    full = httpx.get(live_server.url + frames[1]["url"])
    assert full.status_code == 200 and full.headers["content-type"] == "image/png"
    thumb = httpx.get(live_server.url + frames[1]["url"] + "?w=120")
    assert thumb.headers["content-type"] == "image/jpeg"
    assert len(thumb.content) < len(full.content)

    for bad in ("../config.yaml", "_index.json", "nope.png"):
        assert httpx.get(f"{live_server.url}/regions/wien/history/{bad}").status_code == 404


def test_storage_relocation_moves_frames(live_server):
    live_server.ensure_frames("wien", 2)
    before = live_server.frames("wien")
    target = live_server.storage_root / "data2" / "ha_satellite"
    (live_server.storage_root / "data2").mkdir()

    info = httpx.get(f"{live_server.url}/api/storage").json()
    assert str(target) in [c["path"] for c in info["candidates"]]
    assert info["current"] == str(live_server.data_dir / "frames")

    response = httpx.post(
        f"{live_server.url}/api/storage",
        json={"frames_dir": str(target), "move_existing": True, "history_minutes": 120},
        timeout=30,
    )
    assert response.status_code == 200, response.text
    assert response.json()["moved_frames"] >= len(before)
    assert _stored(live_server)["storage"]["frames_dir"] == str(target)
    assert _stored(live_server)["history"]["history_minutes"] == 120
    assert (target / "wien" / before[0]["filename"]).exists()
    assert not (live_server.data_dir / "frames" / "wien").exists()
    after = {f["filename"] for f in live_server.frames("wien")}
    assert {f["filename"] for f in before} <= after
    assert httpx.get(f"{live_server.url}/regions/wien/latest.png").status_code == 200

    # Zurück auf den Standard: leerer Pfad.
    response = httpx.post(f"{live_server.url}/api/storage", json={"frames_dir": ""}, timeout=30)
    assert response.json()["current"] == str(live_server.data_dir / "frames")
    assert _stored(live_server)["storage"]["frames_dir"] == ""


def test_storage_rejects_relative_and_unwritable_paths(live_server):
    response = httpx.post(f"{live_server.url}/api/storage", json={"frames_dir": "relative"})
    assert response.status_code == 400
    locked = live_server.storage_root / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        response = httpx.post(
            f"{live_server.url}/api/storage", json={"frames_dir": str(locked / "sub")}
        )
        assert response.status_code == 400
        assert "nicht beschreibbar" in response.json()["detail"]
        assert _stored_or_default(live_server) == ""
    finally:
        locked.chmod(0o700)


def _stored_or_default(server) -> str:
    if not server.config_file.exists():
        return ""
    return _stored(server)["storage"]["frames_dir"]


def test_logs_endpoint_shows_render_activity(live_server):
    live_server.ensure_frames("wien", 1)
    entries = httpx.get(f"{live_server.url}/api/logs").json()["entries"]
    messages = [e["message"] for e in entries]
    assert any("Render wien fertig" in m for m in messages)
    last_id = entries[-1]["id"]
    newer = httpx.get(f"{live_server.url}/api/logs?after={last_id}").json()["entries"]
    assert all(e["id"] > last_id for e in newer)
    text = httpx.get(f"{live_server.url}/api/logs?format=text&limit=0").text
    assert "Render wien" in text
    assert (live_server.data_dir / "logs" / "ha_satellite.log").exists()


def test_live_redirect_and_mjpeg_stream(live_server):
    live_server.ensure_frames("wien", 1)
    response = httpx.get(f"{live_server.url}/live/wien", follow_redirects=False)
    assert response.status_code in (302, 307)
    assert response.headers["location"] == "/#live=wien"
    assert httpx.get(f"{live_server.url}/live/nope", follow_redirects=False).status_code == 404

    with httpx.stream("GET", f"{live_server.url}/regions/wien/mjpeg", timeout=10) as stream:
        assert stream.headers["content-type"].startswith("multipart/x-mixed-replace")
        chunk = next(stream.iter_bytes())
        assert b"--frame" in chunk
