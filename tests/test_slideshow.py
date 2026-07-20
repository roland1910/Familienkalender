"""Tests for the photo slideshow (app.slideshow + storage photo methods).

Covers path-traversal validation (../, absolute escapes, symlinks), the
recursive image scan (non-images and #recycle/hidden dirs skipped, symlinks
not followed), the rotation-with-memory pick, the admin API and the
id-based image endpoint. All filesystem work happens under tmp_path with
MEDIA_ROOT pointed there — no real /media needed.
"""

import os
import shutil
import struct
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import slideshow
from app.main import app
from app.storage import Storage, default_db_path


@pytest.fixture
def media_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A fake /media rooted at tmp_path/media, with MEDIA_ROOT pointed at it."""
    root = tmp_path / "media"
    root.mkdir()
    monkeypatch.setenv("MEDIA_ROOT", str(root))
    return root


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, media_root: Path) -> TestClient:
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    return TestClient(app, client=("127.0.0.1", 50000))


@pytest.fixture
def storage(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Storage:
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    return Storage(default_db_path())


def _make_image(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # A tiny but non-empty file; content is irrelevant to the scanner.
    path.write_bytes(b"\xff\xd8\xff\xe0fake-jpeg")


# -- hand-built minimal EXIF JPEGs (no image library in the test deps) -------


def _jpeg_with_exif_payload(tiff: bytes) -> bytes:
    """A minimal JPEG: SOI + one APP1 segment carrying the given TIFF blob."""
    payload = b"Exif\x00\x00" + tiff
    return b"\xff\xd8" + b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload + b"\xff\xd9"


def _tiff_datetime_original(dt: str, order: str = "<") -> bytes:
    """TIFF blob: IFD0 -> ExifIFD pointer -> DateTimeOriginal (0x9003)."""
    prefix = b"II" if order == "<" else b"MM"
    ascii_bytes = dt.encode("ascii") + b"\x00"
    header = prefix + struct.pack(order + "H", 42) + struct.pack(order + "I", 8)
    # IFD0 at 8 (len 18: count + 1 entry + next ptr) -> Exif IFD at 26,
    # Exif IFD (len 18) -> string data at 44.
    ifd0 = (
        struct.pack(order + "H", 1)
        + struct.pack(order + "HHII", 0x8769, 4, 1, 26)
        + struct.pack(order + "I", 0)
    )
    exif_ifd = (
        struct.pack(order + "H", 1)
        + struct.pack(order + "HHII", 0x9003, 2, len(ascii_bytes), 44)
        + struct.pack(order + "I", 0)
    )
    return header + ifd0 + exif_ifd + ascii_bytes


def _tiff_datetime_ifd0(dt: str, order: str = "<") -> bytes:
    """TIFF blob with only the fallback DateTime tag (0x0132) in IFD0."""
    prefix = b"II" if order == "<" else b"MM"
    ascii_bytes = dt.encode("ascii") + b"\x00"
    header = prefix + struct.pack(order + "H", 42) + struct.pack(order + "I", 8)
    # IFD0 at 8 (len 18) -> string data at 26.
    ifd0 = (
        struct.pack(order + "H", 1)
        + struct.pack(order + "HHII", 0x0132, 2, len(ascii_bytes), 26)
        + struct.pack(order + "I", 0)
    )
    return header + ifd0 + ascii_bytes


def _tiff_both_tags(original: str, fallback: str) -> bytes:
    """TIFF with both DateTime (IFD0) and DateTimeOriginal (Exif IFD), LE."""
    order = "<"
    orig_bytes = original.encode("ascii") + b"\x00"
    fall_bytes = fallback.encode("ascii") + b"\x00"
    header = b"II" + struct.pack(order + "H", 42) + struct.pack(order + "I", 8)
    # IFD0 at 8 with two entries (len 2 + 24 + 4 = 30) -> Exif IFD at 38
    # (len 18) -> fallback string at 56, original string at 76.
    ifd0 = (
        struct.pack(order + "H", 2)
        + struct.pack(order + "HHII", 0x0132, 2, len(fall_bytes), 56)
        + struct.pack(order + "HHII", 0x8769, 4, 1, 38)
        + struct.pack(order + "I", 0)
    )
    exif_ifd = (
        struct.pack(order + "H", 1)
        + struct.pack(order + "HHII", 0x9003, 2, len(orig_bytes), 76)
        + struct.pack(order + "I", 0)
    )
    return header + ifd0 + exif_ifd + fall_bytes + orig_bytes


def _write_exif_jpeg(path: Path, dt: str, order: str = "<") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_jpeg_with_exif_payload(_tiff_datetime_original(dt, order)))


class TestNormalizeMediaDir:
    def test_accepts_a_directory_below_the_root(self, media_root: Path) -> None:
        (media_root / "Familie").mkdir()
        result = slideshow.normalize_media_dir(str(media_root / "Familie"))
        assert Path(result) == (media_root / "Familie").resolve()

    def test_accepts_a_relative_path(self, media_root: Path) -> None:
        (media_root / "Urlaube").mkdir()
        result = slideshow.normalize_media_dir("Urlaube")
        assert Path(result) == (media_root / "Urlaube").resolve()

    def test_accepts_the_root_itself(self, media_root: Path) -> None:
        result = slideshow.normalize_media_dir(str(media_root))
        assert Path(result) == media_root.resolve()

    def test_rejects_dotdot_traversal(self, media_root: Path) -> None:
        with pytest.raises(slideshow.InvalidMediaPathError):
            slideshow.normalize_media_dir(str(media_root / ".." / "secret"))

    def test_rejects_relative_dotdot(self, media_root: Path) -> None:
        with pytest.raises(slideshow.InvalidMediaPathError):
            slideshow.normalize_media_dir("../etc")

    def test_rejects_absolute_path_outside_root(self, tmp_path: Path, media_root: Path) -> None:
        outside = tmp_path / "outside"
        outside.mkdir()
        with pytest.raises(slideshow.InvalidMediaPathError):
            slideshow.normalize_media_dir(str(outside))

    def test_rejects_empty(self, media_root: Path) -> None:
        with pytest.raises(slideshow.InvalidMediaPathError):
            slideshow.normalize_media_dir("")

    @pytest.mark.skipif(
        not hasattr(os, "symlink"), reason="symlinks unsupported on this platform"
    )
    def test_rejects_symlink_escaping_the_root(self, tmp_path: Path, media_root: Path) -> None:
        outside = tmp_path / "outside"
        outside.mkdir()
        link = media_root / "escape"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            pytest.skip("symlink creation not permitted on this platform")
        with pytest.raises(slideshow.InvalidMediaPathError):
            slideshow.normalize_media_dir(str(link))


class TestIterMedia:
    def test_finds_images_recursively_and_skips_unknown_types(self, media_root: Path) -> None:
        _make_image(media_root / "a.jpg")
        _make_image(media_root / "sub" / "b.JPEG")
        _make_image(media_root / "sub" / "deep" / "c.png")
        _make_image(media_root / "d.webp")
        (media_root / "movie.mpg").write_bytes(b"unsupported container")
        (media_root / "notes.txt").write_text("hi")
        found = {Path(p).name for p, _, _ in slideshow.iter_media([str(media_root)])}
        assert found == {"a.jpg", "b.JPEG", "c.png", "d.webp"}

    def test_indexes_videos_with_their_kind(self, media_root: Path) -> None:
        """Videos are always indexed — the slideshow_videos switch only
        governs delivery, so toggling it never needs a full rescan."""
        _make_image(media_root / "a.jpg")
        _make_image(media_root / "clip.mp4")
        _make_image(media_root / "sub" / "urlaub.MOV")
        _make_image(media_root / "old.m4v")
        found = {Path(p).name: kind for p, _, kind in slideshow.iter_media([str(media_root)])}
        assert found == {
            "a.jpg": "image",
            "clip.mp4": "video",
            "urlaub.MOV": "video",
            "old.m4v": "video",
        }

    def test_skips_recycle_and_hidden_directories(self, media_root: Path) -> None:
        _make_image(media_root / "keep.jpg")
        _make_image(media_root / "#recycle" / "trash.jpg")
        _make_image(media_root / ".hidden" / "secret.jpg")
        _make_image(media_root / "@eaDir" / "thumb.jpg")
        found = {Path(p).name for p, _, _ in slideshow.iter_media([str(media_root)])}
        assert found == {"keep.jpg"}

    def test_respects_the_limit(self, media_root: Path) -> None:
        for i in range(5):
            _make_image(media_root / f"img{i}.jpg")
        found = list(slideshow.iter_media([str(media_root)], limit=3))
        assert len(found) == 3

    @pytest.mark.skipif(
        not hasattr(os, "symlink"), reason="symlinks unsupported on this platform"
    )
    def test_does_not_follow_directory_symlinks(self, tmp_path: Path, media_root: Path) -> None:
        _make_image(media_root / "real.jpg")
        outside = tmp_path / "outside"
        _make_image(outside / "escaped.jpg")
        try:
            (media_root / "link").symlink_to(outside, target_is_directory=True)
        except OSError:
            pytest.skip("symlink creation not permitted on this platform")
        found = {Path(p).name for p, _, _ in slideshow.iter_media([str(media_root)])}
        assert found == {"real.jpg"}


class TestMediaKind:
    @pytest.mark.parametrize("name", ["a.jpg", "a.JPEG", "a.png", "a.WEBP"])
    def test_image_extensions(self, name: str) -> None:
        assert slideshow.media_kind(name) == "image"

    @pytest.mark.parametrize("name", ["a.mp4", "a.MOV", "a.m4v"])
    def test_video_extensions(self, name: str) -> None:
        assert slideshow.media_kind(name) == "video"

    @pytest.mark.parametrize("name", ["a.mpg", "a.avi", "a.txt", "a", "a.mp4.exe"])
    def test_unknown_extensions(self, name: str) -> None:
        assert slideshow.media_kind(name) is None


class TestRotation:
    def test_pick_marks_shown_and_resets_when_all_shown(self, storage: Storage) -> None:
        storage.replace_photos(
            [("/media/a.jpg", 1.0, "image"), ("/media/b.jpg", 2.0, "image")]
        )
        # A deterministic rng: always pick the first eligible row.
        seq = iter([0.0, 0.0, 0.0, 0.0])
        rng = lambda: next(seq)  # noqa: E731
        first = storage.pick_next_photo(rng)
        second = storage.pick_next_photo(rng)
        assert {first["name"], second["name"]} == {"a.jpg", "b.jpg"}
        # Both shown now — the next pick must reset and hand one out again.
        third = storage.pick_next_photo(rng)
        assert third is not None
        assert third["name"] in {"a.jpg", "b.jpg"}

    def test_pick_on_empty_index_returns_none(self, storage: Storage) -> None:
        assert storage.pick_next_photo() is None

    def test_replace_photos_resets_shown(self, storage: Storage) -> None:
        storage.replace_photos([("/media/a.jpg", 1.0, "image")])
        storage.pick_next_photo()  # marks a.jpg shown
        storage.replace_photos(
            [("/media/a.jpg", 1.0, "image"), ("/media/b.jpg", 2.0, "image")]
        )
        # After a rescan both are unshown again → two distinct picks.
        names = {storage.pick_next_photo()["name"], storage.pick_next_photo()["name"]}
        assert names == {"a.jpg", "b.jpg"}

    def test_pick_reports_the_kind(self, storage: Storage) -> None:
        storage.replace_photos([("/media/clip.mp4", 1.0, "video")])
        assert storage.pick_next_photo()["kind"] == "video"

    def test_images_only_never_picks_a_video(self, storage: Storage) -> None:
        storage.replace_photos(
            [("/media/a.jpg", 1.0, "image"), ("/media/clip.mp4", 2.0, "video")]
        )
        picked = [storage.pick_next_photo(kinds=("image",))["name"] for _ in range(6)]
        assert set(picked) == {"a.jpg"}

    def test_images_only_rotation_does_not_stall_on_videos(self, storage: Storage) -> None:
        """The reset must key on the *eligible* kinds only: with videos off,
        the never-pickable video rows must not keep the cycle from resetting."""
        storage.replace_photos(
            [
                ("/media/a.jpg", 1.0, "image"),
                ("/media/b.jpg", 2.0, "image"),
                ("/media/clip.mp4", 3.0, "video"),
            ]
        )
        names = [
            storage.pick_next_photo(kinds=("image",))["name"] for _ in range(10)
        ]
        assert set(names) == {"a.jpg", "b.jpg"}
        # Ten picks over two images means the cycle reset repeatedly.
        assert names.count("a.jpg") == 5

    def test_both_kinds_eligible_hands_out_both(self, storage: Storage) -> None:
        storage.replace_photos(
            [("/media/a.jpg", 1.0, "image"), ("/media/clip.mp4", 2.0, "video")]
        )
        kinds = {storage.pick_next_photo()["kind"] for _ in range(4)}
        assert kinds == {"image", "video"}

    def test_turning_videos_on_does_not_replay_just_shown_images(
        self, storage: Storage
    ) -> None:
        """Resetting only the eligible kinds keeps the images-only cycle
        intact when videos come back: the untouched video row is picked
        first, not an image that was shown moments ago."""
        storage.replace_photos(
            [("/media/a.jpg", 1.0, "image"), ("/media/clip.mp4", 2.0, "video")]
        )
        storage.pick_next_photo(kinds=("image",))  # a.jpg shown, video untouched
        assert storage.pick_next_photo()["name"] == "clip.mp4"

    def test_empty_kinds_yields_nothing(self, storage: Storage) -> None:
        storage.replace_photos([("/media/a.jpg", 1.0, "image")])
        assert storage.pick_next_photo(kinds=()) is None

    def test_rows_from_an_older_schema_count_as_images(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The additive migration backfills kind='image' — correct, because
        before Etappe 33 only images were ever indexed."""
        import sqlite3

        monkeypatch.setenv("DATA_DIR", str(tmp_path / "legacy"))
        db_path = default_db_path()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "CREATE TABLE photos (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " path TEXT NOT NULL UNIQUE, mtime REAL NOT NULL,"
                " shown INTEGER NOT NULL DEFAULT 0)"
            )
            conn.execute("INSERT INTO photos (path, mtime) VALUES ('/media/old.jpg', 1.0)")
        legacy = Storage(db_path)
        picked = legacy.pick_next_photo(kinds=("image",))
        assert picked is not None
        assert picked["kind"] == "image"
        # Idempotent: opening the same DB again must not fail on the ALTER.
        assert Storage(db_path).count_photos() == 1


class TestAdminApi:
    def test_get_defaults_empty(self, client: TestClient) -> None:
        response = client.get("/api/admin/slideshow")
        assert response.status_code == 200
        payload = response.json()
        assert payload["dirs"] == []
        assert payload["photo_count"] == 0

    def test_put_stores_dirs_and_scans(self, client: TestClient, media_root: Path) -> None:
        _make_image(media_root / "Familie" / "x.jpg")
        _make_image(media_root / "Familie" / "y.png")
        response = client.put(
            "/api/admin/slideshow", json={"dirs": [str(media_root / "Familie")]}
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["photo_count"] == 2
        assert len(payload["dirs"]) == 1

    def test_put_rejects_traversal(self, client: TestClient, media_root: Path) -> None:
        response = client.put("/api/admin/slideshow", json={"dirs": ["../etc"]})
        assert response.status_code == 400
        assert "Medienordner" in response.json()["detail"]

    def test_put_rejects_too_many_dirs(self, client: TestClient, media_root: Path) -> None:
        # One over MAX_SLIDESHOW_DIRS — Pydantic's max_length on the field
        # rejects the oversized list before any path is touched (422).
        too_many = [str(media_root / f"d{i}") for i in range(slideshow.MAX_SLIDESHOW_DIRS + 1)]
        response = client.put("/api/admin/slideshow", json={"dirs": too_many})
        assert response.status_code == 422

    def test_put_rejects_overlong_path(self, client: TestClient, media_root: Path) -> None:
        # A single path longer than MAX_DIR_PATH_LENGTH is rejected (400).
        overlong = str(media_root / ("x" * (slideshow.MAX_DIR_PATH_LENGTH + 1)))
        response = client.put("/api/admin/slideshow", json={"dirs": [overlong]})
        assert response.status_code == 400
        assert "zu lang" in response.json()["detail"]

    def test_put_deduplicates(self, client: TestClient, media_root: Path) -> None:
        (media_root / "Fotos").mkdir()
        target = str(media_root / "Fotos")
        response = client.put("/api/admin/slideshow", json={"dirs": [target, target]})
        assert response.status_code == 200
        assert len(response.json()["dirs"]) == 1

    def test_rescan_endpoint(self, client: TestClient, media_root: Path) -> None:
        _make_image(media_root / "Alben" / "p.jpg")
        client.put("/api/admin/slideshow", json={"dirs": [str(media_root / "Alben")]})
        _make_image(media_root / "Alben" / "q.jpg")
        response = client.post("/api/admin/slideshow/rescan")
        assert response.status_code == 200
        assert response.json()["photo_count"] == 2

    def test_dirs_browser_lists_subdirectories(self, client: TestClient, media_root: Path) -> None:
        (media_root / "Familie").mkdir()
        (media_root / "Urlaube").mkdir()
        (media_root / "#recycle").mkdir()
        (media_root / ".hidden").mkdir()
        response = client.get("/api/admin/slideshow/dirs")
        assert response.status_code == 200
        names = [d["name"] for d in response.json()["dirs"]]
        assert names == ["Familie", "Urlaube"]

    def test_dirs_browser_rejects_traversal(self, client: TestClient, media_root: Path) -> None:
        response = client.get("/api/admin/slideshow/dirs", params={"path": "../etc"})
        assert response.status_code == 400

    def test_dirs_browser_rejects_traversal_below_a_valid_subpath(
        self, client: TestClient, media_root: Path
    ) -> None:
        # A path that is textually rooted at a real, valid subdirectory but
        # walks back out via "../.." must be rejected just like a traversal
        # from the root — normalize_media_dir resolves the whole path before
        # checking the boundary, so the navigation endpoint cannot be tricked
        # by prefixing a legitimate-looking subpath in front of "..".
        (media_root / "Photos" / "Urlaub").mkdir(parents=True)
        escaping_path = str(media_root / "Photos" / "Urlaub" / ".." / ".." / ".." / "etc")
        response = client.get("/api/admin/slideshow/dirs", params={"path": escaping_path})
        assert response.status_code == 400

    def test_dirs_browser_lists_nested_path(self, client: TestClient, media_root: Path) -> None:
        # Navigating into a subdirectory lists *its* children, not the root's.
        (media_root / "Photos" / "Urlaub").mkdir(parents=True)
        (media_root / "Photos" / "Freunde").mkdir()
        response = client.get(
            "/api/admin/slideshow/dirs", params={"path": str(media_root / "Photos")}
        )
        assert response.status_code == 200
        payload = response.json()
        names = [d["name"] for d in payload["dirs"]]
        assert names == ["Freunde", "Urlaub"]

    def test_dirs_browser_reports_root_and_parent(
        self, client: TestClient, media_root: Path
    ) -> None:
        (media_root / "Photos" / "Urlaub").mkdir(parents=True)
        # At the media root, parent is null (cannot navigate above it).
        root_resp = client.get("/api/admin/slideshow/dirs").json()
        assert root_resp["media_root"] == str(media_root)
        assert root_resp["base"] == str(media_root)
        assert root_resp["parent"] is None
        # One level down, parent points back at the media root.
        sub_resp = client.get(
            "/api/admin/slideshow/dirs",
            params={"path": str(media_root / "Photos")},
        ).json()
        assert sub_resp["parent"] == str(media_root)
        # Two levels down, parent is the intermediate directory.
        deep_resp = client.get(
            "/api/admin/slideshow/dirs",
            params={"path": str(media_root / "Photos" / "Urlaub")},
        ).json()
        assert deep_resp["parent"] == str(media_root / "Photos")


class TestScanAvailabilityGuard:
    """A scan must never wipe the index because the share is not mounted.

    Real incident (Etappe 31): after a system restore the add-on started and
    scanned before the CIFS share was mounted under /media. The scan found 0
    photos and replaced the ~114k-entry index with the empty set, so the
    kiosk slideshow showed nothing but black.
    """

    def test_missing_directory_keeps_the_index(
        self, client: TestClient, media_root: Path
    ) -> None:
        album = media_root / "Album"
        _make_image(album / "a.jpg")
        _make_image(album / "b.jpg")
        client.put("/api/admin/slideshow", json={"dirs": [str(album)]})
        assert client.get("/api/admin/slideshow").json()["photo_count"] == 2

        # The "share" disappears (unmounted) — the configured dir is gone.
        shutil.rmtree(album)
        payload = client.post("/api/admin/slideshow/rescan").json()
        assert payload["photo_count"] == 2  # index untouched
        assert payload["unavailable_dirs"] == 1
        assert payload["scan_skipped"] is True

    def test_available_but_empty_directory_empties_the_index(
        self, client: TestClient, media_root: Path
    ) -> None:
        # The legitimate case: the folder is reachable and really has no
        # photos left. Then the index *must* shrink to zero.
        album = media_root / "Album"
        _make_image(album / "a.jpg")
        client.put("/api/admin/slideshow", json={"dirs": [str(album)]})
        (album / "a.jpg").unlink()
        payload = client.post("/api/admin/slideshow/rescan").json()
        assert payload["photo_count"] == 0
        assert payload["unavailable_dirs"] == 0
        assert payload["scan_skipped"] is False

    def test_no_configured_directories_empties_the_index(
        self, client: TestClient, media_root: Path
    ) -> None:
        # Roland removed every folder — an empty index is the correct answer,
        # not a "share unavailable" skip.
        album = media_root / "Album"
        _make_image(album / "a.jpg")
        client.put("/api/admin/slideshow", json={"dirs": [str(album)]})
        payload = client.put("/api/admin/slideshow", json={"dirs": []}).json()
        assert payload["photo_count"] == 0
        assert payload["unavailable_dirs"] == 0
        assert payload["scan_skipped"] is False

    def test_partial_availability_keeps_the_unavailable_directorys_photos(
        self, client: TestClient, media_root: Path
    ) -> None:
        here = media_root / "Hier"
        gone = media_root / "Weg"
        _make_image(here / "a.jpg")
        _make_image(gone / "b.jpg")
        _make_image(gone / "c.jpg")
        client.put(
            "/api/admin/slideshow", json={"dirs": [str(here), str(gone)]}
        )
        assert client.get("/api/admin/slideshow").json()["photo_count"] == 3

        # One share vanishes, the other gains a photo: the available dir is
        # rescanned, the unavailable one keeps its indexed photos.
        shutil.rmtree(gone)
        _make_image(here / "d.jpg")
        payload = client.post("/api/admin/slideshow/rescan").json()
        assert payload["photo_count"] == 4  # a + d (rescanned) + b + c (kept)
        assert payload["unavailable_dirs"] == 1
        assert payload["scan_skipped"] is False

    def test_photos_outside_every_configured_directory_are_dropped(
        self, client: TestClient, media_root: Path
    ) -> None:
        # Keeping is scoped to *unavailable configured* dirs only: an entry
        # from a directory that is no longer configured at all still goes.
        old = media_root / "Alt"
        new = media_root / "Neu"
        _make_image(old / "a.jpg")
        _make_image(new / "b.jpg")
        client.put("/api/admin/slideshow", json={"dirs": [str(old)]})
        payload = client.put("/api/admin/slideshow", json={"dirs": [str(new)]}).json()
        assert payload["photo_count"] == 1

    def test_admin_get_reports_the_last_scan_state(
        self, client: TestClient, media_root: Path
    ) -> None:
        album = media_root / "Album"
        _make_image(album / "a.jpg")
        client.put("/api/admin/slideshow", json={"dirs": [str(album)]})
        shutil.rmtree(album)
        client.post("/api/admin/slideshow/rescan")
        # The flag survives into a plain GET (the admin UI reads it there).
        payload = client.get("/api/admin/slideshow").json()
        assert payload["unavailable_dirs"] == 1
        assert payload["scan_skipped"] is True


class TestImageEndpoint:
    def test_serves_image_by_id(self, client: TestClient, media_root: Path) -> None:
        _make_image(media_root / "Bilder" / "photo.jpg")
        client.put("/api/admin/slideshow", json={"dirs": [str(media_root / "Bilder")]})
        nxt = client.get("/api/slideshow/next")
        assert nxt.status_code == 200
        photo_id = nxt.json()["id"]
        assert nxt.json()["name"] == "photo.jpg"
        image = client.get(f"/api/slideshow/image/{photo_id}")
        assert image.status_code == 200
        assert image.headers["content-type"] == "image/jpeg"
        # Photos may be cached briefly — deliberately NOT the no-cache
        # policy of the app delivery (static assets/HTML).
        assert image.headers["cache-control"] == "private, max-age=60"

    def test_unknown_id_is_404(self, client: TestClient) -> None:
        assert client.get("/api/slideshow/image/999999").status_code == 404

    def test_next_on_empty_index_is_404(self, client: TestClient) -> None:
        assert client.get("/api/slideshow/next").status_code == 404

    def test_vanished_file_is_404_and_removed(self, client: TestClient, media_root: Path) -> None:
        img = media_root / "Weg" / "gone.jpg"
        _make_image(img)
        client.put("/api/admin/slideshow", json={"dirs": [str(media_root / "Weg")]})
        photo_id = client.get("/api/slideshow/next").json()["id"]
        img.unlink()
        assert client.get(f"/api/slideshow/image/{photo_id}").status_code == 404
        # The stale entry is gone: the index is now empty.
        assert client.get("/api/admin/slideshow").json()["photo_count"] == 0

    @pytest.mark.skipif(
        not hasattr(os, "symlink"), reason="symlinks unsupported on this platform"
    )
    def test_indexed_file_swapped_for_escaping_symlink_is_404(
        self, client: TestClient, tmp_path: Path, media_root: Path
    ) -> None:
        # Serve-time TOCTOU guard: a file that was a plain image at index time
        # is replaced, after the scan, by a symlink pointing outside /media
        # (the classic /etc/passwd swap). The image endpoint must refuse it —
        # 404, index cleanup — and never leak the symlink target's bytes.
        img = media_root / "Album" / "pic.jpg"
        _make_image(img)
        client.put("/api/admin/slideshow", json={"dirs": [str(media_root / "Album")]})
        photo_id = client.get("/api/slideshow/next").json()["id"]

        secret = tmp_path / "secret.txt"
        secret.write_text("TOP-SECRET-OUTSIDE-MEDIA")
        img.unlink()
        try:
            img.symlink_to(secret)
        except OSError:
            pytest.skip("symlink creation not permitted on this platform")

        response = client.get(f"/api/slideshow/image/{photo_id}")
        assert response.status_code == 404
        assert b"TOP-SECRET-OUTSIDE-MEDIA" not in response.content
        # Stale entry dropped, symmetric to the vanished-file path.
        assert client.get("/api/admin/slideshow").json()["photo_count"] == 0

    @pytest.mark.skipif(
        not hasattr(os, "symlink"), reason="symlinks unsupported on this platform"
    )
    def test_indexed_file_swapped_for_symlink_inside_media_is_404(
        self, client: TestClient, media_root: Path
    ) -> None:
        # Even a symlink that stays *inside* /media is refused at serve time:
        # the is_symlink() check rejects it regardless of target, so a swapped
        # entry never streams via an indirection the scanner did not vet.
        img = media_root / "Album" / "pic.jpg"
        _make_image(img)
        target = media_root / "Album" / "other.jpg"
        _make_image(target)
        client.put("/api/admin/slideshow", json={"dirs": [str(media_root / "Album")]})
        # Find the id of pic.jpg specifically.
        photo_id = None
        for _ in range(2):
            picked = client.get("/api/slideshow/next").json()
            if picked["name"] == "pic.jpg":
                photo_id = picked["id"]
        assert photo_id is not None
        img.unlink()
        try:
            img.symlink_to(target)
        except OSError:
            pytest.skip("symlink creation not permitted on this platform")
        assert client.get(f"/api/slideshow/image/{photo_id}").status_code == 404


class TestExifTakenAt:
    """The hand-rolled EXIF parser (untrusted input, must never raise)."""

    def test_datetime_original_little_endian(self, media_root: Path) -> None:
        img = media_root / "x.jpg"
        _write_exif_jpeg(img, "2019:08:16 17:30:05", order="<")
        taken = slideshow.photo_taken_at(str(img))
        assert taken == slideshow.TakenAt(year=2019, month=8, day=16, hour=17, minute=30)

    def test_datetime_original_big_endian(self, media_root: Path) -> None:
        img = media_root / "x.jpg"
        _write_exif_jpeg(img, "2021:12:31 23:59:59", order=">")
        taken = slideshow.photo_taken_at(str(img))
        assert taken == slideshow.TakenAt(year=2021, month=12, day=31, hour=23, minute=59)

    def test_fallback_datetime_tag_in_ifd0(self, media_root: Path) -> None:
        img = media_root / "x.jpg"
        img.write_bytes(_jpeg_with_exif_payload(_tiff_datetime_ifd0("2020:02:29 08:15:00")))
        taken = slideshow.photo_taken_at(str(img))
        assert taken == slideshow.TakenAt(year=2020, month=2, day=29, hour=8, minute=15)

    def test_datetime_original_wins_over_fallback(self, media_root: Path) -> None:
        img = media_root / "x.jpg"
        img.write_bytes(
            _jpeg_with_exif_payload(
                _tiff_both_tags("2018:05:04 12:00:00", "2023:01:01 00:00:00")
            )
        )
        taken = slideshow.photo_taken_at(str(img))
        assert taken is not None
        assert (taken.year, taken.month, taken.day) == (2018, 5, 4)

    def test_invalid_exif_date_is_ignored(self, media_root: Path) -> None:
        # Month 13 — structurally fine EXIF, semantically invalid date.
        img = media_root / "x.jpg"
        _write_exif_jpeg(img, "2019:13:16 17:30:05")
        assert slideshow.photo_taken_at(str(img)) is None

    def test_truncated_jpeg_returns_none(self, media_root: Path) -> None:
        img = media_root / "x.jpg"
        full = _jpeg_with_exif_payload(_tiff_datetime_original("2019:08:16 17:30:05"))
        img.write_bytes(full[: len(full) // 2])
        assert slideshow.photo_taken_at(str(img)) is None

    def test_hostile_ifd_offset_returns_none(self, media_root: Path) -> None:
        # IFD0 offset points far beyond the buffer — must not raise.
        tiff = b"II" + struct.pack("<H", 42) + struct.pack("<I", 0xFFFFFF0)
        img = media_root / "x.jpg"
        img.write_bytes(_jpeg_with_exif_payload(tiff))
        assert slideshow.photo_taken_at(str(img)) is None

    def test_hostile_value_offset_returns_none(self, media_root: Path) -> None:
        # The ASCII value offset points beyond the buffer.
        order = "<"
        header = b"II" + struct.pack(order + "H", 42) + struct.pack(order + "I", 8)
        ifd0 = (
            struct.pack(order + "H", 1)
            + struct.pack(order + "HHII", 0x0132, 2, 20, 0xFFFF00)
            + struct.pack(order + "I", 0)
        )
        img = media_root / "x.jpg"
        img.write_bytes(_jpeg_with_exif_payload(header + ifd0))
        assert slideshow.photo_taken_at(str(img)) is None

    def test_garbage_bytes_return_none(self, media_root: Path) -> None:
        img = media_root / "x.jpg"
        img.write_bytes(b"\xff\xd8\xff\xe0not really exif at all")
        assert slideshow.photo_taken_at(str(img)) is None

    def test_missing_file_returns_none(self, media_root: Path) -> None:
        assert slideshow.photo_taken_at(str(media_root / "nope.jpg")) is None


class TestFilenameTakenAt:
    """Date extraction from filename patterns (with plausibility checks)."""

    def test_compact_datetime_with_underscore(self, media_root: Path) -> None:
        img = media_root / "IMG_20190816_173005.jpg"
        _make_image(img)
        assert slideshow.photo_taken_at(str(img)) == slideshow.TakenAt(
            year=2019, month=8, day=16, hour=17, minute=30
        )

    def test_compact_datetime_with_dash_and_pxl_prefix(self, media_root: Path) -> None:
        img = media_root / "PXL_20230102-070809.png"
        _make_image(img)
        assert slideshow.photo_taken_at(str(img)) == slideshow.TakenAt(
            year=2023, month=1, day=2, hour=7, minute=8
        )

    def test_dashed_date_with_dotted_time(self, media_root: Path) -> None:
        img = media_root / "2021-06-05 14.33.10.jpg"
        _make_image(img)
        assert slideshow.photo_taken_at(str(img)) == slideshow.TakenAt(
            year=2021, month=6, day=5, hour=14, minute=33
        )

    def test_whatsapp_pattern_date_only(self, media_root: Path) -> None:
        img = media_root / "IMG-20180923-WA0007.jpg"
        _make_image(img)
        assert slideshow.photo_taken_at(str(img)) == slideshow.TakenAt(
            year=2018, month=9, day=23
        )

    def test_bare_date(self, media_root: Path) -> None:
        img = media_root / "20170501.webp"
        _make_image(img)
        assert slideshow.photo_taken_at(str(img)) == slideshow.TakenAt(
            year=2017, month=5, day=1
        )

    def test_invalid_month_returns_none(self, media_root: Path) -> None:
        img = media_root / "20211345_999999.jpg"
        _make_image(img)
        assert slideshow.photo_taken_at(str(img)) is None

    def test_valid_date_with_invalid_time_yields_date_only(self, media_root: Path) -> None:
        img = media_root / "20210505_996060.jpg"
        _make_image(img)
        assert slideshow.photo_taken_at(str(img)) == slideshow.TakenAt(
            year=2021, month=5, day=5
        )

    def test_implausible_year_returns_none(self, media_root: Path) -> None:
        img = media_root / "12345678.jpg"
        _make_image(img)
        assert slideshow.photo_taken_at(str(img)) is None

    def test_future_year_beyond_next_returns_none(self, media_root: Path) -> None:
        year = date.today().year + 3
        img = media_root / f"{year}0101.jpg"
        _make_image(img)
        assert slideshow.photo_taken_at(str(img)) is None

    def test_long_digit_run_does_not_match(self, media_root: Path) -> None:
        # A longer digit run (e.g. a phone-scan id) must not be misread as
        # a date somewhere inside it.
        img = media_root / "123201908160001234.jpg"
        _make_image(img)
        assert slideshow.photo_taken_at(str(img)) is None


class TestFolderYearTakenAt:
    def test_year_folder_below_media_root(self, media_root: Path) -> None:
        img = media_root / "2015" / "Urlaub" / "strand.jpg"
        _make_image(img)
        assert slideshow.photo_taken_at(str(img)) == slideshow.TakenAt(year=2015)

    def test_nested_year_folder(self, media_root: Path) -> None:
        img = media_root / "Fotos" / "2008" / "winter.png"
        _make_image(img)
        assert slideshow.photo_taken_at(str(img)) == slideshow.TakenAt(year=2008)

    def test_non_year_folders_yield_none(self, media_root: Path) -> None:
        img = media_root / "Familie" / "Ausflug" / "wald.jpg"
        _make_image(img)
        assert slideshow.photo_taken_at(str(img)) is None

    def test_partial_year_like_folder_does_not_match(self, media_root: Path) -> None:
        # Only an exact four-digit 19xx/20xx segment counts.
        img = media_root / "Urlaub2019x" / "meer.jpg"
        _make_image(img)
        assert slideshow.photo_taken_at(str(img)) is None


class TestTakenAtPriority:
    def test_exif_wins_over_filename_and_folder(self, media_root: Path) -> None:
        img = media_root / "2001" / "20200101_120000.jpg"
        _write_exif_jpeg(img, "2019:08:16 17:30:05")
        taken = slideshow.photo_taken_at(str(img))
        assert taken is not None
        assert taken.year == 2019

    def test_filename_wins_over_folder(self, media_root: Path) -> None:
        img = media_root / "2001" / "20200101_120000.jpg"
        _make_image(img)  # no usable EXIF
        taken = slideshow.photo_taken_at(str(img))
        assert taken is not None
        assert taken.year == 2020

    def test_folder_is_last_resort(self, media_root: Path) -> None:
        img = media_root / "2001" / "strand.jpg"
        _make_image(img)
        assert slideshow.photo_taken_at(str(img)) == slideshow.TakenAt(year=2001)


class TestNextPhotoTakenField:
    def test_next_carries_structured_taken(self, client: TestClient, media_root: Path) -> None:
        _write_exif_jpeg(media_root / "Bilder" / "x.jpg", "2019:08:16 17:30:05")
        client.put("/api/admin/slideshow", json={"dirs": [str(media_root / "Bilder")]})
        payload = client.get("/api/slideshow/next").json()
        assert payload["taken"] == {
            "year": 2019, "month": 8, "day": 16, "hour": 17, "minute": 30,
        }

    def test_next_taken_is_null_when_nothing_extractable(
        self, client: TestClient, media_root: Path
    ) -> None:
        _make_image(media_root / "Bilder" / "photo.jpg")
        client.put("/api/admin/slideshow", json={"dirs": [str(media_root / "Bilder")]})
        payload = client.get("/api/slideshow/next").json()
        assert payload["taken"] is None

    def test_next_does_not_leak_the_path(self, client: TestClient, media_root: Path) -> None:
        _make_image(media_root / "Bilder" / "photo.jpg")
        client.put("/api/admin/slideshow", json={"dirs": [str(media_root / "Bilder")]})
        payload = client.get("/api/slideshow/next").json()
        assert set(payload.keys()) == {"id", "name", "kind", "taken", "folders"}


class TestPhotoFolders:
    """Path segments below the media root (for the top-left overlay)."""

    def test_nested_folders(self, media_root: Path) -> None:
        img = media_root / "Photos" / "2019" / "Urlaub" / "IMG.jpg"
        _make_image(img)
        assert slideshow.photo_folders(str(img)) == ["Photos", "2019", "Urlaub"]

    def test_photo_directly_in_root_yields_empty_list(self, media_root: Path) -> None:
        img = media_root / "IMG.jpg"
        _make_image(img)
        assert slideshow.photo_folders(str(img)) == []

    def test_single_folder(self, media_root: Path) -> None:
        img = media_root / "Familie" / "IMG.jpg"
        _make_image(img)
        assert slideshow.photo_folders(str(img)) == ["Familie"]

    def test_path_outside_root_yields_empty_list(self, tmp_path: Path, media_root: Path) -> None:
        img = tmp_path / "outside" / "IMG.jpg"
        _make_image(img)
        assert slideshow.photo_folders(str(img)) == []

    def test_next_response_carries_folders(self, client: TestClient, media_root: Path) -> None:
        _make_image(media_root / "Photos" / "2019" / "Urlaub" / "IMG.jpg")
        client.put(
            "/api/admin/slideshow", json={"dirs": [str(media_root / "Photos")]}
        )
        payload = client.get("/api/slideshow/next").json()
        assert payload["folders"] == ["Photos", "2019", "Urlaub"]

    def test_next_response_empty_folders_at_root(
        self, client: TestClient, media_root: Path
    ) -> None:
        _make_image(media_root / "IMG.jpg")
        client.put("/api/admin/slideshow", json={"dirs": [str(media_root)]})
        payload = client.get("/api/slideshow/next").json()
        assert payload["folders"] == []


class TestVideoDelivery:
    """The slideshow_videos switch governs delivery only — never the index."""

    def _album_with_both(self, client: TestClient, media_root: Path) -> Path:
        album = media_root / "Album"
        _make_image(album / "a.jpg")
        _make_image(album / "clip.mp4")
        client.put("/api/admin/slideshow", json={"dirs": [str(album)]})
        return album

    def test_videos_are_indexed_even_while_switched_off(
        self, client: TestClient, media_root: Path
    ) -> None:
        self._album_with_both(client, media_root)
        payload = client.get("/api/admin/slideshow").json()
        assert payload["videos"] == "off"  # conservative default
        assert payload["photo_count"] == 2  # ...but both are in the index

    def test_off_never_hands_out_a_video(
        self, client: TestClient, media_root: Path
    ) -> None:
        self._album_with_both(client, media_root)
        seen = [client.get("/api/slideshow/next").json() for _ in range(8)]
        assert {item["name"] for item in seen} == {"a.jpg"}
        assert {item["kind"] for item in seen} == {"image"}

    def test_off_with_only_videos_indexed_is_404(
        self, client: TestClient, media_root: Path
    ) -> None:
        album = media_root / "Filme"
        _make_image(album / "clip.mp4")
        client.put("/api/admin/slideshow", json={"dirs": [str(album)]})
        assert client.get("/api/admin/slideshow").json()["photo_count"] == 1
        assert client.get("/api/slideshow/next").status_code == 404

    def test_on_hands_out_both_kinds(
        self, client: TestClient, media_root: Path
    ) -> None:
        album = self._album_with_both(client, media_root)
        client.put("/api/admin/slideshow", json={"dirs": [str(album)], "videos": "on"})
        seen = {client.get("/api/slideshow/next").json()["kind"] for _ in range(6)}
        assert seen == {"image", "video"}

    def test_switching_videos_on_keeps_the_index(
        self, client: TestClient, media_root: Path
    ) -> None:
        album = self._album_with_both(client, media_root)
        payload = client.put(
            "/api/admin/slideshow", json={"dirs": [str(album)], "videos": "on"}
        ).json()
        assert payload["videos"] == "on"
        assert payload["photo_count"] == 2

    def test_taken_at_from_a_video_filename(
        self, client: TestClient, media_root: Path
    ) -> None:
        album = media_root / "Album"
        _make_image(album / "VID_20190816_173005.mp4")
        client.put("/api/admin/slideshow", json={"dirs": [str(album)], "videos": "on"})
        payload = client.get("/api/slideshow/next").json()
        assert payload["kind"] == "video"
        assert payload["taken"] == {
            "year": 2019,
            "month": 8,
            "day": 16,
            "hour": 17,
            "minute": 30,
        }


class TestVideoAdminApi:
    def test_put_stores_the_video_switch(
        self, client: TestClient, media_root: Path
    ) -> None:
        response = client.put("/api/admin/slideshow", json={"dirs": [], "videos": "on"})
        assert response.status_code == 200
        assert response.json()["videos"] == "on"
        assert client.get("/api/admin/slideshow").json()["videos"] == "on"

    def test_put_without_videos_leaves_it_untouched(
        self, client: TestClient, media_root: Path
    ) -> None:
        client.put("/api/admin/slideshow", json={"dirs": [], "videos": "on"})
        client.put("/api/admin/slideshow", json={"dirs": []})
        assert client.get("/api/admin/slideshow").json()["videos"] == "on"

    @pytest.mark.parametrize("value", ["yes", "ON", "1", "", "vielleicht"])
    def test_put_rejects_an_invalid_video_value(
        self, client: TestClient, media_root: Path, value: str
    ) -> None:
        response = client.put("/api/admin/slideshow", json={"dirs": [], "videos": value})
        assert response.status_code == 400

    def test_invalid_video_value_does_not_change_the_dirs(
        self, client: TestClient, media_root: Path
    ) -> None:
        album = media_root / "Album"
        _make_image(album / "a.jpg")
        client.put("/api/admin/slideshow", json={"dirs": [str(album)]})
        client.put("/api/admin/slideshow", json={"dirs": [], "videos": "kaputt"})
        assert client.get("/api/admin/slideshow").json()["dirs"] == [str(album)]

    def test_invalid_stored_value_falls_back_to_off(
        self, client: TestClient, media_root: Path
    ) -> None:
        # Defense in depth: a value smuggled in by another write path must
        # not start handing out videos.
        Storage(default_db_path()).set_setting("slideshow_videos", "ja")
        assert client.get("/api/admin/slideshow").json()["videos"] == "off"


class TestContentTypes:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("a.jpg", "image/jpeg"),
            ("a.jpeg", "image/jpeg"),
            ("a.png", "image/png"),
            ("a.webp", "image/webp"),
            ("a.mp4", "video/mp4"),
            ("a.m4v", "video/mp4"),
            ("a.mov", "video/quicktime"),
        ],
    )
    def test_content_type_per_extension(
        self, client: TestClient, media_root: Path, name: str, expected: str
    ) -> None:
        album = media_root / "Album"
        _make_image(album / name)
        client.put("/api/admin/slideshow", json={"dirs": [str(album)], "videos": "on"})
        photo_id = client.get("/api/slideshow/next").json()["id"]
        response = client.get(f"/api/slideshow/image/{photo_id}")
        assert response.status_code == 200
        assert response.headers["content-type"] == expected


class TestRangeRequests:
    """Byte-range support on the media endpoint.

    Starlette's FileResponse implements this; the tests pin the behaviour so
    a dependency bump cannot silently drop it. Without ranges a browser
    seeking in a video would stall or be forced to download the whole file.
    """

    BODY = b"0123456789abcdef"

    @pytest.fixture
    def video_id(self, client: TestClient, media_root: Path) -> int:
        album = media_root / "Album"
        album.mkdir(parents=True, exist_ok=True)
        (album / "clip.mp4").write_bytes(self.BODY)
        client.put("/api/admin/slideshow", json={"dirs": [str(album)], "videos": "on"})
        return client.get("/api/slideshow/next").json()["id"]

    def test_full_request_advertises_range_support(
        self, client: TestClient, video_id: int
    ) -> None:
        response = client.get(f"/api/slideshow/image/{video_id}")
        assert response.status_code == 200
        assert response.headers["accept-ranges"] == "bytes"
        assert response.content == self.BODY

    def test_partial_request_returns_206_and_the_exact_bytes(
        self, client: TestClient, video_id: int
    ) -> None:
        response = client.get(
            f"/api/slideshow/image/{video_id}", headers={"Range": "bytes=4-7"}
        )
        assert response.status_code == 206
        assert response.content == b"4567"
        assert response.headers["content-range"] == f"bytes 4-7/{len(self.BODY)}"
        assert response.headers["content-length"] == "4"

    def test_open_ended_range_runs_to_the_end(
        self, client: TestClient, video_id: int
    ) -> None:
        response = client.get(
            f"/api/slideshow/image/{video_id}", headers={"Range": "bytes=10-"}
        )
        assert response.status_code == 206
        assert response.content == self.BODY[10:]

    def test_suffix_range_returns_the_tail(
        self, client: TestClient, video_id: int
    ) -> None:
        response = client.get(
            f"/api/slideshow/image/{video_id}", headers={"Range": "bytes=-4"}
        )
        assert response.status_code == 206
        assert response.content == self.BODY[-4:]

    def test_range_beyond_the_file_is_416(
        self, client: TestClient, video_id: int
    ) -> None:
        response = client.get(
            f"/api/slideshow/image/{video_id}", headers={"Range": "bytes=999-1200"}
        )
        assert response.status_code == 416
        assert response.headers["content-range"] == f"bytes */{len(self.BODY)}"

    def test_images_support_ranges_too(
        self, client: TestClient, media_root: Path
    ) -> None:
        album = media_root / "Album"
        album.mkdir(parents=True, exist_ok=True)
        (album / "a.jpg").write_bytes(self.BODY)
        client.put("/api/admin/slideshow", json={"dirs": [str(album)]})
        photo_id = client.get("/api/slideshow/next").json()["id"]
        response = client.get(
            f"/api/slideshow/image/{photo_id}", headers={"Range": "bytes=0-3"}
        )
        assert response.status_code == 206
        assert response.content == b"0123"
