from pathlib import Path

from ha_satellite.buffer import BufferManager, RingBuffer


def _png_bytes(marker: bytes = b"\x89PNG\r\n\x1a\n") -> bytes:
    return marker + b"fake-image-data"


def test_add_frame_creates_file(tmp_path: Path):
    buf = RingBuffer(region_dir=tmp_path / "wien", max_frames=5, max_storage_mb=10)
    frame = buf.add_frame(_png_bytes())
    assert (tmp_path / "wien" / frame.filename).exists()
    assert len(buf) == 1


def test_max_frames_enforced(tmp_path: Path):
    buf = RingBuffer(region_dir=tmp_path / "wien", max_frames=3, max_storage_mb=100)
    filenames = []
    for _ in range(5):
        frame = buf.add_frame(_png_bytes())
        filenames.append(frame.filename)

    assert len(buf) == 3
    # Die ältesten zwei Dateien müssen gelöscht worden sein.
    region_dir = tmp_path / "wien"
    remaining = {p.name for p in region_dir.glob("*.png")}
    assert remaining == set(filenames[-3:])


def test_latest_returns_most_recent(tmp_path: Path):
    buf = RingBuffer(region_dir=tmp_path / "wien", max_frames=5, max_storage_mb=100)
    buf.add_frame(_png_bytes())
    last = buf.add_frame(_png_bytes())
    assert buf.latest().filename == last.filename


def test_get_index_zero_is_latest(tmp_path: Path):
    buf = RingBuffer(region_dir=tmp_path / "wien", max_frames=5, max_storage_mb=100)
    buf.add_frame(_png_bytes())
    last = buf.add_frame(_png_bytes())
    assert buf.get(0).filename == last.filename


def test_storage_limit_enforced(tmp_path: Path):
    big_payload = b"0" * (1024 * 100)  # 100 KB je Frame
    buf = RingBuffer(region_dir=tmp_path / "wien", max_frames=100, max_storage_mb=0.25)
    for _ in range(10):
        buf.add_frame(big_payload)
    # 0.25 MB Limit / 100 KB pro Frame => höchstens 2-3 Frames bleiben übrig
    assert len(buf) <= 3
    total_size = sum(p.stat().st_size for p in (tmp_path / "wien").glob("*.png"))
    assert total_size <= 0.25 * 1024 * 1024


def test_orphan_files_removed_on_reload(tmp_path: Path):
    region_dir = tmp_path / "wien"
    buf = RingBuffer(region_dir=region_dir, max_frames=5, max_storage_mb=100)
    buf.add_frame(_png_bytes())

    # Simuliert eine verwaiste Datei, die nicht im Index steht.
    (region_dir / "orphan.png").write_bytes(_png_bytes())
    assert (region_dir / "orphan.png").exists()

    # Ein neuer RingBuffer über dasselbe Verzeichnis muss beim Laden aufräumen.
    buf2 = RingBuffer(region_dir=region_dir, max_frames=5, max_storage_mb=100)
    buf2._remove_orphans()
    assert not (region_dir / "orphan.png").exists()


def test_orphan_files_removed_automatically_on_add_frame(tmp_path: Path):
    region_dir = tmp_path / "wien"
    buf = RingBuffer(region_dir=region_dir, max_frames=5, max_storage_mb=100)
    buf.add_frame(_png_bytes())

    # Simuliert eine verwaiste Datei, die nicht im Index steht.
    (region_dir / "orphan.png").write_bytes(_png_bytes())
    assert (region_dir / "orphan.png").exists()

    # Der reguläre add_frame-Pfad (über _cleanup()) muss die Waise ebenfalls
    # entfernen, ohne dass _remove_orphans() manuell aufgerufen wird.
    buf.add_frame(_png_bytes())
    assert not (region_dir / "orphan.png").exists()


def test_index_survives_reload(tmp_path: Path):
    region_dir = tmp_path / "wien"
    buf = RingBuffer(region_dir=region_dir, max_frames=5, max_storage_mb=100)
    buf.add_frame(_png_bytes())
    buf.add_frame(_png_bytes())

    buf2 = RingBuffer(region_dir=region_dir, max_frames=5, max_storage_mb=100)
    assert len(buf2) == 2


def test_buffer_manager_reuses_instance(tmp_path: Path):
    manager = BufferManager(tmp_path)
    buf1 = manager.get("wien", max_frames=5, max_storage_mb=10)
    buf2 = manager.get("wien", max_frames=5, max_storage_mb=10)
    assert buf1 is buf2


def test_buffer_manager_updates_limits(tmp_path: Path):
    manager = BufferManager(tmp_path)
    buf = manager.get("wien", max_frames=5, max_storage_mb=10)
    for _ in range(5):
        buf.add_frame(_png_bytes())
    assert len(buf) == 5

    manager.get("wien", max_frames=2, max_storage_mb=10)
    assert len(buf) == 2


def test_relocate_moves_and_merges_regions(tmp_path):
    from datetime import datetime, timedelta, timezone

    from ha_satellite.buffer import BufferManager

    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    old = BufferManager(tmp_path / "old")
    old.get("wien", 10, 100).add_frame(b"a", t0)
    old.get("wien", 10, 100).add_frame(b"c", t0 + timedelta(minutes=2))
    new_base = tmp_path / "new"
    existing = BufferManager(new_base).get("wien", 10, 100)
    existing.add_frame(b"b", t0 + timedelta(minutes=1))

    moved = old.relocate(new_base, move_existing=True)

    assert moved == 2
    assert old.base_dir == new_base
    assert not (tmp_path / "old" / "wien").exists()
    buf = old.get("wien", 10, 100)
    contents = [f.path(buf.region_dir).read_bytes() for f in buf.frames_newest_first()]
    assert contents == [b"c", b"b", b"a"]


def test_relocate_without_move_keeps_old_frames(tmp_path):
    from ha_satellite.buffer import BufferManager

    manager = BufferManager(tmp_path / "old")
    manager.get("wien", 10, 100).add_frame(b"a")
    assert manager.relocate(tmp_path / "new", move_existing=False) == 0
    assert len(manager.get("wien", 10, 100)) == 0
    assert any((tmp_path / "old" / "wien").glob("*.png"))


def test_frames_keep_origin_and_insertion_order(tmp_path):
    from datetime import datetime, timezone

    buf = RingBuffer(tmp_path / "wien", max_frames=5)
    newer = datetime(2026, 9, 26, 13, 40, tzinfo=timezone.utc)
    older = datetime(2026, 9, 26, 13, 27, tzinfo=timezone.utc)
    buf.add_frame(b"rss", timestamp=newer, source="msg_seviri", composite="a")
    # Quellenwechsel auf eine ältere Aufnahme: trotzdem neuester Frame.
    buf.add_frame(b"0deg", timestamp=older, source="msg_seviri_0deg", composite="a")
    # Gleicher Zeitstempel (anderes Komposit) überschreibt nichts.
    buf.add_frame(b"0deg-b", timestamp=older, source="msg_seviri_0deg", composite="b")

    assert [f.source for f in buf.frames_newest_first()] == ["msg_seviri_0deg", "msg_seviri_0deg", "msg_seviri"]
    assert buf.latest().composite == "b"
    assert len({f.filename for f in buf.frames_newest_first()}) == 3
    assert buf.latest().path(buf.region_dir).read_bytes() == b"0deg-b"

    reloaded = RingBuffer(tmp_path / "wien", max_frames=5)
    assert [f.as_dict() for f in reloaded.frames_newest_first()] == [
        f.as_dict() for f in buf.frames_newest_first()
    ]
