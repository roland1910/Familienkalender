"""Tests for the photo slideshow (app.slideshow + storage photo methods).

Covers path-traversal validation (../, absolute escapes, symlinks), the
recursive image scan (non-images and #recycle/hidden dirs skipped, symlinks
not followed), the rotation-with-memory pick, the admin API and the
id-based image endpoint. All filesystem work happens under tmp_path with
MEDIA_ROOT pointed there — no real /media needed.
"""

import os
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


class TestIterImages:
    def test_finds_images_recursively_and_skips_non_images(self, media_root: Path) -> None:
        _make_image(media_root / "a.jpg")
        _make_image(media_root / "sub" / "b.JPEG")
        _make_image(media_root / "sub" / "deep" / "c.png")
        _make_image(media_root / "d.webp")
        (media_root / "movie.mpg").write_bytes(b"not an image")
        (media_root / "notes.txt").write_text("hi")
        found = {Path(p).name for p, _ in slideshow.iter_images([str(media_root)])}
        assert found == {"a.jpg", "b.JPEG", "c.png", "d.webp"}

    def test_skips_recycle_and_hidden_directories(self, media_root: Path) -> None:
        _make_image(media_root / "keep.jpg")
        _make_image(media_root / "#recycle" / "trash.jpg")
        _make_image(media_root / ".hidden" / "secret.jpg")
        _make_image(media_root / "@eaDir" / "thumb.jpg")
        found = {Path(p).name for p, _ in slideshow.iter_images([str(media_root)])}
        assert found == {"keep.jpg"}

    def test_respects_the_limit(self, media_root: Path) -> None:
        for i in range(5):
            _make_image(media_root / f"img{i}.jpg")
        found = list(slideshow.iter_images([str(media_root)], limit=3))
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
        found = {Path(p).name for p, _ in slideshow.iter_images([str(media_root)])}
        assert found == {"real.jpg"}


class TestRotation:
    def test_pick_marks_shown_and_resets_when_all_shown(self, storage: Storage) -> None:
        storage.replace_photos([("/media/a.jpg", 1.0), ("/media/b.jpg", 2.0)])
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
        storage.replace_photos([("/media/a.jpg", 1.0)])
        storage.pick_next_photo()  # marks a.jpg shown
        storage.replace_photos([("/media/a.jpg", 1.0), ("/media/b.jpg", 2.0)])
        # After a rescan both are unshown again → two distinct picks.
        names = {storage.pick_next_photo()["name"], storage.pick_next_photo()["name"]}
        assert names == {"a.jpg", "b.jpg"}


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
