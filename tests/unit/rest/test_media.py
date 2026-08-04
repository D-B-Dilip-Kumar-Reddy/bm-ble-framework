"""Unit tests for :mod:`bmd_camera.rest.media`.

No real network — `resolve_active_mount`/`find_highest_still_index`/
`wait_for_new_still` take a `RestCameraSession`-like object exposing only
the three methods they actually call (`storage_state`, `mount_names`,
`path_exists`), faked directly here rather than reusing
`tests/unit/rest/test_rest_session.py`'s lower-level `FakeRestClient`.
"""

from __future__ import annotations

import asyncio

import pytest

from bmd_camera.rest.media import (
    derive_still_prefix,
    find_highest_still_index,
    resolve_active_mount,
    resolve_mount_path,
    wait_for_new_still,
)
from bmd_camera.rest.session import StorageDevice, StorageState


class TestDeriveStillPrefix:
    def test_strips_directory_extension_and_clip_index(self):
        assert derive_still_prefix("/mnt/sd0/A001/A001_07311253_C001.mov") == "A001_07311253"

    def test_handles_bare_filename(self):
        assert derive_still_prefix("A001_07311253_C042.mov") == "A001_07311253"

    def test_raises_for_unexpected_shape(self):
        with pytest.raises(ValueError, match="does not match"):
            derive_still_prefix("/mnt/sd0/A001/not_a_clip_filename.mov")


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
        self.path_exists_calls: list[str] = []
        self._existing_paths: set[str] = set()

    def set_existing(self, *paths: str) -> None:
        self._existing_paths = set(paths)

    async def storage_state(self) -> StorageState:
        return self._storage

    async def mount_names(self) -> tuple[str, ...]:
        return self._mounts

    async def path_exists(self, path: str) -> bool:
        self.path_exists_calls.append(path)
        return path in self._existing_paths


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


class TestFindHighestStillIndex:
    @pytest.mark.asyncio
    async def test_returns_none_when_no_stills_exist(self):
        session = FakeMediaSession(storage=_storage_with_active("A001"), mounts=("A001-sd1",))

        result = await find_highest_still_index(session, "/mounts/A001-sd1/", "A001_0001")

        assert result is None

    @pytest.mark.asyncio
    async def test_finds_highest_contiguous_index(self):
        session = FakeMediaSession(storage=_storage_with_active("A001"), mounts=("A001-sd1",))
        session.set_existing(
            "/mounts/A001-sd1/Stills/A001_0001_S001.dng",
            "/mounts/A001-sd1/Stills/A001_0001_S002.dng",
            "/mounts/A001-sd1/Stills/A001_0001_S003.dng",
        )

        result = await find_highest_still_index(session, "/mounts/A001-sd1/", "A001_0001")

        assert result == 3

    @pytest.mark.asyncio
    async def test_finds_braw_extension_too(self):
        """POCKET_6K_PRO v8.6 BRAW stills are .braw, not .dng —
        docs/ble/photo_capture.md §8.4."""
        session = FakeMediaSession(storage=_storage_with_active("A001"), mounts=("A001-sd1",))
        session.set_existing("/mounts/A001-sd1/Stills/A001_0001_S001.braw")

        result = await find_highest_still_index(session, "/mounts/A001-sd1/", "A001_0001")

        assert result == 1

    @pytest.mark.asyncio
    async def test_stops_at_first_gap(self):
        session = FakeMediaSession(storage=_storage_with_active("A001"), mounts=("A001-sd1",))
        session.set_existing(
            "/mounts/A001-sd1/Stills/A001_0001_S001.dng",
            "/mounts/A001-sd1/Stills/A001_0001_S002.dng",
            # S003 deliberately missing
            "/mounts/A001-sd1/Stills/A001_0001_S004.dng",
        )

        result = await find_highest_still_index(session, "/mounts/A001-sd1/", "A001_0001")

        assert result == 2

    @pytest.mark.asyncio
    async def test_respects_max_index_bound(self):
        session = FakeMediaSession(storage=_storage_with_active("A001"), mounts=("A001-sd1",))
        session.set_existing(
            *[f"/mounts/A001-sd1/Stills/A001_0001_S{i:03d}.dng" for i in range(1, 20)]
        )

        result = await find_highest_still_index(
            session, "/mounts/A001-sd1/", "A001_0001", max_index=5
        )

        assert result == 5


class TestWaitForNewStill:
    @pytest.mark.asyncio
    async def test_returns_target_index_when_already_present(self):
        session = FakeMediaSession(storage=_storage_with_active("A001"), mounts=("A001-sd1",))
        session.set_existing("/mounts/A001-sd1/Stills/A001_0001_S002.dng")

        result = await wait_for_new_still(
            session, "/mounts/A001-sd1/", "A001_0001", baseline_index=1, timeout_s=0.2
        )

        assert result == 2

    @pytest.mark.asyncio
    async def test_none_baseline_targets_index_one(self):
        session = FakeMediaSession(storage=_storage_with_active("A001"), mounts=("A001-sd1",))
        session.set_existing("/mounts/A001-sd1/Stills/A001_0001_S001.dng")

        result = await wait_for_new_still(
            session, "/mounts/A001-sd1/", "A001_0001", baseline_index=None, timeout_s=0.2
        )

        assert result == 1

    @pytest.mark.asyncio
    async def test_returns_none_on_timeout(self):
        session = FakeMediaSession(storage=_storage_with_active("A001"), mounts=("A001-sd1",))

        result = await wait_for_new_still(
            session,
            "/mounts/A001-sd1/",
            "A001_0001",
            baseline_index=1,
            timeout_s=0.15,
            poll_interval_s=0.05,
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_finds_still_that_appears_mid_poll(self):
        session = FakeMediaSession(storage=_storage_with_active("A001"), mounts=("A001-sd1",))

        async def appear_later():
            await asyncio.sleep(0.05)
            session.set_existing("/mounts/A001-sd1/Stills/A001_0001_S002.dng")

        asyncio.create_task(appear_later())

        result = await wait_for_new_still(
            session,
            "/mounts/A001-sd1/",
            "A001_0001",
            baseline_index=1,
            timeout_s=1.0,
            poll_interval_s=0.02,
        )

        assert result == 2
