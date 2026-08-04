"""Unit tests for :mod:`bmd_camera.rest.media`.

No real network — `resolve_active_mount`/`stills_marker`/`wait_for_new_still`
take a `RestCameraSession`-like object exposing only the methods they
actually call (`storage_state`, `mount_names`, `list_mount`), faked
directly here rather than reusing
`tests/unit/rest/test_rest_session.py`'s lower-level `FakeRestClient`.
"""

from __future__ import annotations

import asyncio

import pytest

from bmd_camera.rest.media import (
    resolve_active_mount,
    resolve_mount_path,
    stills_marker,
    wait_for_new_still,
)
from bmd_camera.rest.session import StorageDevice, StorageState


class TestResolveMountPath:
    def test_single_mount_used_unconditionally(self):
        """No volume needed at all when there's only one mount — sidesteps
        the unconfirmed deviceName->mount-suffix mapping entirely."""
        assert resolve_mount_path(("A001-sd1",), volume=None) == "/mounts/A001-sd1/"

    def test_multiple_mounts_narrowed_by_volume_prefix(self):
        path = resolve_mount_path(("A001-sd1", "B002-sd2"), volume="B002")
        assert path == "/mounts/B002-sd2/"

    def test_raises_when_no_mounts(self):
        with pytest.raises(ValueError, match="no mounts"):
            resolve_mount_path((), volume=None)

    def test_raises_when_multiple_mounts_and_no_volume(self):
        with pytest.raises(ValueError, match="no active volume"):
            resolve_mount_path(("A001-sd1", "B002-sd2"), volume=None)

    def test_raises_when_volume_matches_none(self):
        with pytest.raises(ValueError, match="cannot resolve"):
            resolve_mount_path(("A001-sd1", "B002-sd2"), volume="C003")

    def test_raises_when_volume_matches_more_than_one(self):
        with pytest.raises(ValueError, match="cannot resolve"):
            resolve_mount_path(("A001-sd1", "A001-sd2"), volume="A001")


class FakeMediaSession:
    def __init__(self, *, storage: StorageState, mounts: tuple[str, ...]):
        self._storage = storage
        self._mounts = mounts
        self.list_mount_calls: list[str] = []
        self._mount_entries: dict[str, tuple[dict, ...]] = {}

    def set_mount_entries(self, mount_path: str, entries: tuple[dict, ...]) -> None:
        self._mount_entries[mount_path] = entries

    async def storage_state(self) -> StorageState:
        return self._storage

    async def mount_names(self) -> tuple[str, ...]:
        return self._mounts

    async def list_mount(self, path: str) -> tuple[dict, ...]:
        self.list_mount_calls.append(path)
        return self._mount_entries.get(path, ())


def _storage_with_active(volume: str | None) -> StorageState:
    device = StorageDevice(index=1, device_name="sd0", active=True, total_space=100, volume=volume)
    return StorageState(devices=(device,), active_device=device)


class TestResolveActiveMount:
    @pytest.mark.asyncio
    async def test_combines_storage_state_and_mount_names(self):
        session = FakeMediaSession(storage=_storage_with_active("A001"), mounts=("A001-sd1",))

        assert await resolve_active_mount(session) == "/mounts/A001-sd1/"

    @pytest.mark.asyncio
    async def test_no_active_device_falls_back_to_single_mount(self):
        storage = StorageState(devices=(), active_device=None)
        session = FakeMediaSession(storage=storage, mounts=("A001-sd1",))

        assert await resolve_active_mount(session) == "/mounts/A001-sd1/"

    @pytest.mark.asyncio
    async def test_no_active_device_and_multiple_mounts_raises(self):
        storage = StorageState(devices=(), active_device=None)
        session = FakeMediaSession(storage=storage, mounts=("A001-sd1", "B002-sd2"))

        with pytest.raises(ValueError, match="no active volume"):
            await resolve_active_mount(session)


class TestStillsMarker:
    @pytest.mark.asyncio
    async def test_returns_none_when_no_stills_entry(self):
        session = FakeMediaSession(storage=_storage_with_active("A001"), mounts=("A001-sd1",))
        session.set_mount_entries(
            "/mounts/A001-sd1/",
            ({"name": "A001_07311253_C001.mov", "type": "file", "mtime": "..."},),
        )

        assert await stills_marker(session, "/mounts/A001-sd1/") is None

    @pytest.mark.asyncio
    async def test_returns_mtime_of_stills_entry(self):
        session = FakeMediaSession(storage=_storage_with_active("A001"), mounts=("A001-sd1",))
        session.set_mount_entries(
            "/mounts/A001-sd1/",
            (
                {"name": "Stills", "type": "directory", "mtime": "Fri, 31 Jul 2026 12:54:20"},
                {"name": "A001_07311253_C001.mov", "type": "file", "mtime": "..."},
            ),
        )

        assert await stills_marker(session, "/mounts/A001-sd1/") == "Fri, 31 Jul 2026 12:54:20"

    @pytest.mark.asyncio
    async def test_ignores_a_file_named_stills(self):
        """Only a directory entry named "Stills" counts — a same-named
        file (never observed, but not ruled out) must not be mistaken for
        the real subdirectory."""
        session = FakeMediaSession(storage=_storage_with_active("A001"), mounts=("A001-sd1",))
        session.set_mount_entries(
            "/mounts/A001-sd1/",
            ({"name": "Stills", "type": "file", "mtime": "some mtime"},),
        )

        assert await stills_marker(session, "/mounts/A001-sd1/") is None


class TestWaitForNewStill:
    @pytest.mark.asyncio
    async def test_returns_true_when_marker_already_differs(self):
        session = FakeMediaSession(storage=_storage_with_active("A001"), mounts=("A001-sd1",))
        session.set_mount_entries(
            "/mounts/A001-sd1/",
            ({"name": "Stills", "type": "directory", "mtime": "Tue, 04 Aug 2026 11:26:24"},),
        )

        result = await wait_for_new_still(
            session,
            "/mounts/A001-sd1/",
            "Fri, 31 Jul 2026 12:54:20",
            timeout_s=0.2,
        )

        assert result is True

    @pytest.mark.asyncio
    async def test_none_baseline_confirms_as_soon_as_stills_appears(self):
        """First-ever photo on a card with no prior Stills directory."""
        session = FakeMediaSession(storage=_storage_with_active("A001"), mounts=("A001-sd1",))
        session.set_mount_entries(
            "/mounts/A001-sd1/",
            ({"name": "Stills", "type": "directory", "mtime": "Tue, 04 Aug 2026 11:26:24"},),
        )

        result = await wait_for_new_still(session, "/mounts/A001-sd1/", None, timeout_s=0.2)

        assert result is True

    @pytest.mark.asyncio
    async def test_returns_false_on_timeout_when_marker_unchanged(self):
        session = FakeMediaSession(storage=_storage_with_active("A001"), mounts=("A001-sd1",))
        session.set_mount_entries(
            "/mounts/A001-sd1/",
            ({"name": "Stills", "type": "directory", "mtime": "Fri, 31 Jul 2026 12:54:20"},),
        )

        result = await wait_for_new_still(
            session,
            "/mounts/A001-sd1/",
            "Fri, 31 Jul 2026 12:54:20",
            timeout_s=0.15,
            poll_interval_s=0.05,
        )

        assert result is False

    @pytest.mark.asyncio
    async def test_finds_change_that_appears_mid_poll(self):
        session = FakeMediaSession(storage=_storage_with_active("A001"), mounts=("A001-sd1",))
        session.set_mount_entries(
            "/mounts/A001-sd1/",
            ({"name": "Stills", "type": "directory", "mtime": "Fri, 31 Jul 2026 12:54:20"},),
        )

        async def change_later():
            await asyncio.sleep(0.05)
            session.set_mount_entries(
                "/mounts/A001-sd1/",
                ({"name": "Stills", "type": "directory", "mtime": "Tue, 04 Aug 2026 11:26:24"},),
            )

        asyncio.create_task(change_later())

        result = await wait_for_new_still(
            session,
            "/mounts/A001-sd1/",
            "Fri, 31 Jul 2026 12:54:20",
            timeout_s=1.0,
            poll_interval_s=0.02,
        )

        assert result is True
