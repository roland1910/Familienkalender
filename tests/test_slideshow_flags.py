"""Tests for the slideshow photo flags (Etappe 49): rotation and deletion.

Two things a viewer standing at the kiosk can do to the picture on screen:
turn it (stored as a display rotation, because /media is mounted read-only
and the original file must not be rewritten anyway) and mark it for
deletion. The deletion itself happens later, in the admin area, through
Home Assistant's media source API — the add-on has no write access to the
share itself.

The guard this whole feature hangs on is that the flags live in their own
table keyed by PATH: ``replace_photos`` wipes and rebuilds ``photos`` on
every hourly scan, so anything stored there would be gone within the hour.
``test_flags_survive_a_rescan`` is the test that matters most here.

Every Home Assistant WebSocket call is faked — no test touches the network.
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import ha_media, slideshow
from app.main import app
from app.storage import Storage, default_db_path


@pytest.fixture
def media_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    root = tmp_path / "media"
    root.mkdir()
    monkeypatch.setenv("MEDIA_ROOT", str(root))
    return root


@pytest.fixture
def storage(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Storage:
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    return Storage(default_db_path())


@pytest.fixture
def client(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, media_root: Path
) -> TestClient:
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    return TestClient(app, client=("127.0.0.1", 50000))


def _album(media_root: Path, *names: str) -> Path:
    """Create an album directory with the given files; returns the directory."""
    album = media_root / "Album"
    album.mkdir(parents=True, exist_ok=True)
    for name in names:
        (album / name).write_bytes(b"\xff\xd8\xff\xe0fake-jpeg")
    return album


def _configure(client: TestClient, album: Path) -> None:
    response = client.put("/api/admin/slideshow", json={"dirs": [str(album)]})
    assert response.status_code == 200


def _photo_ids(client: TestClient) -> dict[str, int]:
    """Map filename -> id by draining the rotation once."""
    ids: dict[str, int] = {}
    count = client.get("/api/admin/slideshow").json()["photo_count"]
    for _ in range(count):
        item = client.get("/api/slideshow/next").json()
        ids[item["name"]] = item["id"]
    return ids


# -- storage layer -----------------------------------------------------------


class TestRotationStorage:
    def test_unflagged_photo_has_no_rotation(self, storage: Storage) -> None:
        assert storage.get_photo_rotation("/media/a.jpg") == 0

    @pytest.mark.parametrize("rotation", [0, 90, 180, 270])
    def test_roundtrip_of_every_quarter_turn(
        self, storage: Storage, rotation: int
    ) -> None:
        assert storage.set_photo_rotation("/media/a.jpg", rotation) == rotation
        assert storage.get_photo_rotation("/media/a.jpg") == rotation

    @pytest.mark.parametrize("rotation", [45, -90, 360, 1000])
    def test_values_outside_the_quarter_turns_become_zero(
        self, storage: Storage, rotation: int
    ) -> None:
        assert storage.set_photo_rotation("/media/a.jpg", rotation) == 0
        assert storage.get_photo_rotation("/media/a.jpg") == 0

    def test_a_junk_value_smuggled_into_the_table_reads_as_zero(
        self, storage: Storage
    ) -> None:
        # Defense in depth: re-validated on read, like get_slideshow_dirs.
        storage.set_photo_rotation("/media/a.jpg", 90)
        with storage._connect() as conn:
            conn.execute("UPDATE photo_flags SET rotation = 37")
        assert storage.get_photo_rotation("/media/a.jpg") == 0

    def test_resetting_to_zero_drops_the_row(self, storage: Storage) -> None:
        storage.set_photo_rotation("/media/a.jpg", 90)
        storage.set_photo_rotation("/media/a.jpg", 0)
        assert storage.list_photo_flags() == []

    def test_resetting_to_zero_keeps_a_pending_deletion(
        self, storage: Storage
    ) -> None:
        storage.set_photo_rotation("/media/a.jpg", 90)
        storage.mark_photo_for_deletion("/media/a.jpg")
        storage.set_photo_rotation("/media/a.jpg", 0)
        flags = storage.list_photo_flags()
        assert len(flags) == 1
        assert flags[0]["rotation"] == 0
        assert flags[0]["delete_requested_at"] is not None

    def test_rotations_are_per_path(self, storage: Storage) -> None:
        storage.set_photo_rotation("/media/a.jpg", 90)
        storage.set_photo_rotation("/media/b.jpg", 270)
        assert storage.get_photo_rotation("/media/a.jpg") == 90
        assert storage.get_photo_rotation("/media/b.jpg") == 270


class TestDeleteFlagStorage:
    def test_mark_and_list(self, storage: Storage) -> None:
        storage.replace_photos([("/media/a.jpg", 1.0, "image")])
        marked_at = storage.mark_photo_for_deletion("/media/a.jpg")
        flags = storage.list_photo_flags()
        assert len(flags) == 1
        assert flags[0]["path"] == "/media/a.jpg"
        assert flags[0]["delete_requested_at"] == marked_at
        assert flags[0]["photo_id"] is not None
        assert flags[0]["kind"] == "image"

    def test_marking_twice_keeps_the_first_timestamp(
        self, storage: Storage
    ) -> None:
        first = storage.mark_photo_for_deletion("/media/a.jpg")
        second = storage.mark_photo_for_deletion("/media/a.jpg")
        assert second == first

    def test_unmark_drops_the_row_without_a_rotation(self, storage: Storage) -> None:
        storage.mark_photo_for_deletion("/media/a.jpg")
        storage.unmark_photo_for_deletion("/media/a.jpg")
        assert storage.list_photo_flags() == []

    def test_unmark_keeps_a_rotation(self, storage: Storage) -> None:
        storage.set_photo_rotation("/media/a.jpg", 180)
        storage.mark_photo_for_deletion("/media/a.jpg")
        storage.unmark_photo_for_deletion("/media/a.jpg")
        assert storage.get_photo_rotation("/media/a.jpg") == 180
        assert storage.list_photo_flags()[0]["delete_requested_at"] is None

    def test_unmarking_an_unflagged_photo_is_a_no_op(self, storage: Storage) -> None:
        storage.unmark_photo_for_deletion("/media/never-seen.jpg")
        assert storage.list_photo_flags() == []

    def test_clear_removes_the_whole_row(self, storage: Storage) -> None:
        storage.set_photo_rotation("/media/a.jpg", 90)
        storage.mark_photo_for_deletion("/media/a.jpg")
        storage.clear_photo_flags("/media/a.jpg")
        assert storage.list_photo_flags() == []

    def test_a_flag_without_an_index_row_lists_as_an_orphan(
        self, storage: Storage
    ) -> None:
        storage.mark_photo_for_deletion("/media/gone.jpg")
        flags = storage.list_photo_flags()
        assert flags[0]["photo_id"] is None
        assert flags[0]["kind"] is None


class TestFlagsSurviveARescan:
    def test_flags_survive_a_rescan(self, storage: Storage) -> None:
        """THE core test of this stage.

        ``replace_photos`` deletes every row of ``photos`` — a rotation or a
        delete request stored there would be gone after the next hourly
        scan. Keyed by path in their own table, they survive, even though
        the photo's id changes.
        """
        storage.replace_photos([("/media/a.jpg", 1.0, "image")])
        old_id = storage.pick_next_photo()["id"]
        storage.set_photo_rotation("/media/a.jpg", 90)
        storage.mark_photo_for_deletion("/media/a.jpg")

        # A rescan that finds a second file: the table is rebuilt wholesale.
        storage.replace_photos(
            [("/media/b.jpg", 2.0, "image"), ("/media/a.jpg", 1.0, "image")]
        )

        assert storage.get_photo_rotation("/media/a.jpg") == 90
        flags = storage.list_photo_flags()
        assert [flag["path"] for flag in flags] == ["/media/a.jpg"]
        assert flags[0]["delete_requested_at"] is not None
        # The id really was reassigned — which is why the path is the key.
        assert flags[0]["photo_id"] != old_id or old_id is None


class TestPendingDeletesAreNotShown:
    def test_a_marked_photo_is_never_picked(self, storage: Storage) -> None:
        storage.replace_photos(
            [("/media/a.jpg", 1.0, "image"), ("/media/b.jpg", 2.0, "image")]
        )
        storage.mark_photo_for_deletion("/media/a.jpg")
        picked = {storage.pick_next_photo()["name"] for _ in range(10)}
        assert picked == {"b.jpg"}

    def test_marked_photos_do_not_stall_the_all_shown_reset(
        self, storage: Storage
    ) -> None:
        """Same trap as the switched-off videos: a row that can never be
        picked must also be invisible to the reset, or the cycle spins."""
        storage.replace_photos(
            [
                ("/media/a.jpg", 1.0, "image"),
                ("/media/b.jpg", 2.0, "image"),
                ("/media/doomed.jpg", 3.0, "image"),
            ]
        )
        storage.mark_photo_for_deletion("/media/doomed.jpg")
        names = [storage.pick_next_photo()["name"] for _ in range(10)]
        assert set(names) == {"a.jpg", "b.jpg"}
        assert names.count("a.jpg") == 5

    def test_everything_marked_means_nothing_to_show(self, storage: Storage) -> None:
        storage.replace_photos([("/media/a.jpg", 1.0, "image")])
        storage.mark_photo_for_deletion("/media/a.jpg")
        assert storage.pick_next_photo() is None

    def test_unmarking_brings_the_photo_back(self, storage: Storage) -> None:
        storage.replace_photos([("/media/a.jpg", 1.0, "image")])
        storage.mark_photo_for_deletion("/media/a.jpg")
        assert storage.pick_next_photo() is None
        storage.unmark_photo_for_deletion("/media/a.jpg")
        assert storage.pick_next_photo()["name"] == "a.jpg"


# -- public kiosk API --------------------------------------------------------


class TestRotateEndpoint:
    def test_next_reports_no_rotation_by_default(
        self, client: TestClient, media_root: Path
    ) -> None:
        _configure(client, _album(media_root, "a.jpg"))
        assert client.get("/api/slideshow/next").json()["rotation"] == 0

    def test_rotating_clockwise_steps_by_ninety(
        self, client: TestClient, media_root: Path
    ) -> None:
        _configure(client, _album(media_root, "a.jpg"))
        photo_id = client.get("/api/slideshow/next").json()["id"]
        for expected in (90, 180, 270, 0):
            response = client.post(
                f"/api/slideshow/rotate/{photo_id}", json={"direction": "cw"}
            )
            assert response.status_code == 200
            assert response.json() == {"id": photo_id, "rotation": expected}

    def test_rotating_counter_clockwise_wraps_below_zero(
        self, client: TestClient, media_root: Path
    ) -> None:
        _configure(client, _album(media_root, "a.jpg"))
        photo_id = client.get("/api/slideshow/next").json()["id"]
        response = client.post(
            f"/api/slideshow/rotate/{photo_id}", json={"direction": "ccw"}
        )
        assert response.json()["rotation"] == 270

    def test_the_rotation_is_handed_out_with_the_next_pick(
        self, client: TestClient, media_root: Path
    ) -> None:
        _configure(client, _album(media_root, "a.jpg"))
        photo_id = client.get("/api/slideshow/next").json()["id"]
        client.post(f"/api/slideshow/rotate/{photo_id}", json={"direction": "cw"})
        client.post(f"/api/slideshow/rotate/{photo_id}", json={"direction": "cw"})
        # One photo only — the rotation resets and hands it out again.
        assert client.get("/api/slideshow/next").json()["rotation"] == 180

    def test_the_rotation_survives_a_rescan(
        self, client: TestClient, media_root: Path
    ) -> None:
        album = _album(media_root, "a.jpg")
        _configure(client, album)
        photo_id = client.get("/api/slideshow/next").json()["id"]
        client.post(f"/api/slideshow/rotate/{photo_id}", json={"direction": "ccw"})
        assert client.post("/api/admin/slideshow/rescan").status_code == 200
        assert client.get("/api/slideshow/next").json()["rotation"] == 270

    @pytest.mark.parametrize("direction", ["sideways", "CW", "", "90", "cw ", "left"])
    def test_an_unknown_direction_is_rejected(
        self, client: TestClient, media_root: Path, direction: str
    ) -> None:
        _configure(client, _album(media_root, "a.jpg"))
        photo_id = client.get("/api/slideshow/next").json()["id"]
        response = client.post(
            f"/api/slideshow/rotate/{photo_id}", json={"direction": direction}
        )
        assert response.status_code == 400
        assert client.get("/api/slideshow/next").json()["rotation"] == 0

    def test_a_missing_body_is_rejected(
        self, client: TestClient, media_root: Path
    ) -> None:
        _configure(client, _album(media_root, "a.jpg"))
        photo_id = client.get("/api/slideshow/next").json()["id"]
        assert client.post(f"/api/slideshow/rotate/{photo_id}").status_code == 400

    @pytest.mark.parametrize(
        "raw_id",
        ["+1", " 1", "1_2", "-1", "abc", "1.0", chr(0xFF11)],  # full-width digit one
    )
    def test_a_non_numeric_id_is_rejected_without_touching_the_index(
        self, client: TestClient, media_root: Path, raw_id: str
    ) -> None:
        # Same strict digits-only rule as the weather tile proxy: int() alone
        # would happily swallow "+1", " 1" and even "1_2".
        _configure(client, _album(media_root, "a.jpg"))
        response = client.post(
            f"/api/slideshow/rotate/{raw_id}", json={"direction": "cw"}
        )
        assert response.status_code == 400

    def test_an_unknown_id_is_404(self, client: TestClient, media_root: Path) -> None:
        _configure(client, _album(media_root, "a.jpg"))
        response = client.post(
            "/api/slideshow/rotate/999999", json={"direction": "cw"}
        )
        assert response.status_code == 404


class TestMarkDeleteEndpoint:
    def test_marking_hides_the_photo_from_the_slideshow(
        self, client: TestClient, media_root: Path
    ) -> None:
        _configure(client, _album(media_root, "a.jpg", "b.jpg"))
        ids = _photo_ids(client)
        response = client.post(f"/api/slideshow/mark-delete/{ids['a.jpg']}")
        assert response.status_code == 200
        assert response.json()["delete_requested"] is True
        served = {client.get("/api/slideshow/next").json()["name"] for _ in range(6)}
        assert served == {"b.jpg"}

    def test_marking_is_idempotent(
        self, client: TestClient, media_root: Path
    ) -> None:
        _configure(client, _album(media_root, "a.jpg"))
        photo_id = client.get("/api/slideshow/next").json()["id"]
        first = client.post(f"/api/slideshow/mark-delete/{photo_id}").json()
        second = client.post(f"/api/slideshow/mark-delete/{photo_id}").json()
        assert first == second

    def test_unmarking_brings_the_photo_back(
        self, client: TestClient, media_root: Path
    ) -> None:
        _configure(client, _album(media_root, "a.jpg"))
        photo_id = client.get("/api/slideshow/next").json()["id"]
        client.post(f"/api/slideshow/mark-delete/{photo_id}")
        assert client.get("/api/slideshow/next").status_code == 404
        response = client.delete(f"/api/slideshow/mark-delete/{photo_id}")
        assert response.status_code == 200
        assert response.json()["delete_requested"] is False
        assert client.get("/api/slideshow/next").json()["name"] == "a.jpg"

    def test_the_mark_survives_a_rescan(
        self, client: TestClient, media_root: Path
    ) -> None:
        _configure(client, _album(media_root, "a.jpg", "b.jpg"))
        ids = _photo_ids(client)
        client.post(f"/api/slideshow/mark-delete/{ids['a.jpg']}")
        client.post("/api/admin/slideshow/rescan")
        served = {client.get("/api/slideshow/next").json()["name"] for _ in range(6)}
        assert served == {"b.jpg"}

    @pytest.mark.parametrize("raw_id", ["+1", " 1", "1_2", "abc"])
    def test_a_non_numeric_id_is_rejected(
        self, client: TestClient, media_root: Path, raw_id: str
    ) -> None:
        _configure(client, _album(media_root, "a.jpg"))
        assert client.post(f"/api/slideshow/mark-delete/{raw_id}").status_code == 400
        assert client.delete(f"/api/slideshow/mark-delete/{raw_id}").status_code == 400

    def test_an_unknown_id_is_404(self, client: TestClient, media_root: Path) -> None:
        _configure(client, _album(media_root, "a.jpg"))
        assert client.post("/api/slideshow/mark-delete/999999").status_code == 404


# -- admin API: preview and final deletion -----------------------------------


@pytest.fixture
def ha_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every media_content_id the app would send to Home Assistant."""
    calls: list[str] = []

    async def fake_delete(relative_path: str) -> None:
        calls.append(ha_media.media_content_id(relative_path))

    monkeypatch.setattr(ha_media, "delete_media", fake_delete)
    return calls


@pytest.fixture
def failing_ha(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    async def fake_delete(relative_path: str) -> None:
        calls.append(relative_path)
        raise ha_media.MediaDeleteError("Home Assistant konnte die Datei nicht löschen.")

    monkeypatch.setattr(ha_media, "delete_media", fake_delete)
    return calls


class TestPendingDeletesList:
    def test_empty_by_default(self, client: TestClient, media_root: Path) -> None:
        _configure(client, _album(media_root, "a.jpg"))
        response = client.get("/api/admin/slideshow/pending-deletes")
        assert response.status_code == 200
        assert response.json() == {"items": []}

    def test_lists_the_marked_photo_with_its_relative_path(
        self, client: TestClient, media_root: Path
    ) -> None:
        album = media_root / "Photos" / "2019 Urlaub"
        album.mkdir(parents=True)
        (album / "IMG_20190816_173005.jpg").write_bytes(b"\xff\xd8\xff\xe0x")
        _configure(client, album)
        photo_id = client.get("/api/slideshow/next").json()["id"]
        client.post(f"/api/slideshow/mark-delete/{photo_id}")

        items = client.get("/api/admin/slideshow/pending-deletes").json()["items"]
        assert len(items) == 1
        item = items[0]
        assert item["id"] == photo_id
        assert item["name"] == "IMG_20190816_173005.jpg"
        # Relative to the media root — Roland wants to see where it lives,
        # but the container-absolute path stays inside.
        assert item["path"] == "Photos/2019 Urlaub/IMG_20190816_173005.jpg"
        assert item["folders"] == ["Photos", "2019 Urlaub"]
        assert item["kind"] == "image"
        assert item["marked_at"]
        assert item["taken"]["year"] == 2019

    def test_unmarked_photos_do_not_appear(
        self, client: TestClient, media_root: Path
    ) -> None:
        _configure(client, _album(media_root, "a.jpg", "b.jpg"))
        ids = _photo_ids(client)
        client.post(f"/api/slideshow/mark-delete/{ids['a.jpg']}")
        client.post(f"/api/slideshow/rotate/{ids['b.jpg']}", json={"direction": "cw"})
        items = client.get("/api/admin/slideshow/pending-deletes").json()["items"]
        assert [item["name"] for item in items] == ["a.jpg"]

    def test_withdrawing_from_the_admin_list(
        self, client: TestClient, media_root: Path
    ) -> None:
        _configure(client, _album(media_root, "a.jpg"))
        photo_id = client.get("/api/slideshow/next").json()["id"]
        client.post(f"/api/slideshow/mark-delete/{photo_id}")
        response = client.delete(f"/api/admin/slideshow/pending-deletes/{photo_id}")
        assert response.status_code == 200
        assert client.get("/api/admin/slideshow/pending-deletes").json() == {
            "items": []
        }
        assert client.get("/api/slideshow/next").json()["name"] == "a.jpg"

    def test_an_orphaned_flag_is_swept_up_while_the_share_is_reachable(
        self, client: TestClient, media_root: Path
    ) -> None:
        album = _album(media_root, "a.jpg", "b.jpg")
        _configure(client, album)
        ids = _photo_ids(client)
        client.post(f"/api/slideshow/mark-delete/{ids['a.jpg']}")
        (album / "a.jpg").unlink()
        client.post("/api/admin/slideshow/rescan")  # a.jpg leaves the index

        assert client.get("/api/admin/slideshow/pending-deletes").json() == {
            "items": []
        }
        storage = Storage(default_db_path())
        assert storage.list_photo_flags() == []

    def test_an_orphaned_flag_is_kept_while_the_share_is_unreachable(
        self, client: TestClient, media_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Same lesson as the scan guard: a vanished mount is not a deletion."""
        album = _album(media_root, "a.jpg")
        _configure(client, album)
        photo_id = client.get("/api/slideshow/next").json()["id"]
        client.post(f"/api/slideshow/mark-delete/{photo_id}")
        storage = Storage(default_db_path())
        storage.replace_photos([])  # as if a scan had emptied the index
        monkeypatch.setattr(slideshow, "_dir_is_available", lambda path: False)

        client.get("/api/admin/slideshow/pending-deletes")

        assert [flag["path"] for flag in storage.list_photo_flags()] == [
            str(album / "a.jpg")
        ]


class TestFinalDeletion:
    def _marked_photo(self, client: TestClient, media_root: Path) -> tuple[int, Path]:
        album = media_root / "Photos" / "Grüne Woche #2"
        album.mkdir(parents=True)
        photo = album / "Foto 1.jpg"
        photo.write_bytes(b"\xff\xd8\xff\xe0x")
        _configure(client, album)
        photo_id = client.get("/api/slideshow/next").json()["id"]
        client.post(f"/api/slideshow/mark-delete/{photo_id}")
        return photo_id, photo

    def test_deleting_calls_home_assistant_and_cleans_up(
        self, client: TestClient, media_root: Path, ha_calls: list[str]
    ) -> None:
        photo_id, _ = self._marked_photo(client, media_root)
        response = client.post(
            f"/api/admin/slideshow/pending-deletes/{photo_id}/delete"
        )
        assert response.status_code == 200
        # The path travels verbatim: spaces, umlaut and '#' unencoded.
        assert ha_calls == [
            "media-source://media_source/local/Photos/Grüne Woche #2/Foto 1.jpg"
        ]
        assert client.get("/api/admin/slideshow/pending-deletes").json()["items"] == []
        storage = Storage(default_db_path())
        assert storage.count_photos() == 0
        assert storage.list_photo_flags() == []

    def test_a_photo_that_was_never_marked_is_refused(
        self, client: TestClient, media_root: Path, ha_calls: list[str]
    ) -> None:
        """A guessed id must not be able to delete anything."""
        _configure(client, _album(media_root, "innocent.jpg"))
        photo_id = client.get("/api/slideshow/next").json()["id"]
        response = client.post(
            f"/api/admin/slideshow/pending-deletes/{photo_id}/delete"
        )
        assert response.status_code == 409
        assert ha_calls == []  # no request left the add-on
        assert Storage(default_db_path()).count_photos() == 1

    def test_a_withdrawn_mark_can_no_longer_be_deleted(
        self, client: TestClient, media_root: Path, ha_calls: list[str]
    ) -> None:
        photo_id, _ = self._marked_photo(client, media_root)
        client.delete(f"/api/slideshow/mark-delete/{photo_id}")
        response = client.post(
            f"/api/admin/slideshow/pending-deletes/{photo_id}/delete"
        )
        assert response.status_code == 409
        assert ha_calls == []

    @pytest.mark.parametrize("raw_id", ["+1", " 1", "1_2", "abc"])
    def test_a_non_numeric_id_is_rejected(
        self, client: TestClient, media_root: Path, ha_calls: list[str], raw_id: str
    ) -> None:
        _configure(client, _album(media_root, "a.jpg"))
        response = client.post(
            f"/api/admin/slideshow/pending-deletes/{raw_id}/delete"
        )
        assert response.status_code == 400
        assert ha_calls == []

    def test_an_unknown_id_is_404(
        self, client: TestClient, media_root: Path, ha_calls: list[str]
    ) -> None:
        _configure(client, _album(media_root, "a.jpg"))
        response = client.post("/api/admin/slideshow/pending-deletes/999999/delete")
        assert response.status_code == 404
        assert ha_calls == []

    def test_a_failure_from_home_assistant_leaves_everything_in_place(
        self, client: TestClient, media_root: Path, failing_ha: list[str]
    ) -> None:
        """The file is still on the NAS — so it must stay in the list."""
        photo_id, _ = self._marked_photo(client, media_root)
        response = client.post(
            f"/api/admin/slideshow/pending-deletes/{photo_id}/delete"
        )
        assert response.status_code == 502
        assert "Home Assistant" in response.json()["detail"]
        assert failing_ha  # it really was attempted
        storage = Storage(default_db_path())
        assert storage.count_photos() == 1
        items = client.get("/api/admin/slideshow/pending-deletes").json()["items"]
        assert [item["id"] for item in items] == [photo_id]

    def test_an_indexed_path_outside_the_media_root_is_refused(
        self, client: TestClient, media_root: Path, tmp_path: Path, ha_calls: list[str]
    ) -> None:
        """Nothing outside /media can ever be addressed, even via the index."""
        outsider = tmp_path / "outside.jpg"
        outsider.write_bytes(b"\xff\xd8\xff\xe0x")
        storage = Storage(default_db_path())
        storage.replace_photos([(str(outsider), 1.0, "image")])
        storage.mark_photo_for_deletion(str(outsider))
        # A marked photo is never picked, so read its id from the flag row.
        photo_id = storage.list_photo_flags()[0]["photo_id"]

        response = client.post(
            f"/api/admin/slideshow/pending-deletes/{photo_id}/delete"
        )
        assert response.status_code == 400
        assert ha_calls == []
        assert outsider.exists()
