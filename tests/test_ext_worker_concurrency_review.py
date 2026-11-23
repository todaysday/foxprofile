"""Slow browser-data staging keeps independent catalog operations available."""

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from foxprofile_ext.application import ProtectedApplication
from foxprofile_ext.auth import AuthService
from foxprofile_ext.catalog import ProfileCatalog
from foxprofile_ext.errors import DomainError, ErrorCode
from foxprofile_ext.storage import AssignmentDAO, CatalogOperationDAO, SessionDAO, UserDAO
from foxprofile_ext.workers import profile_store
from src.services.profile import manager as manager_module
from tests.test_ext_distributed_integration import ACTOR, launch
from tests.test_ext_distributed_integration import cluster as cluster


def _catalog_application(cluster, monkeypatch):
    profiles_file = cluster.directory.parent / "profiles.json"
    profiles_file.write_text(json.dumps({cluster.profile.name: cluster.profile.to_dict()}))
    monkeypatch.setattr(manager_module, "DATA_DIR", cluster.directory.parent)
    monkeypatch.setattr(manager_module, "PROFILES_FILE", profiles_file)
    assignments = AssignmentDAO(cluster.db)
    catalog = ProfileCatalog(
        manager_module.ProfileManager(),
        CatalogOperationDAO(cluster.db),
        assignments,
        cluster.coordinator.lock_path,
    )
    monkeypatch.setattr(cluster.coordinator, "profile_by_id", catalog.get_profile)
    monkeypatch.setattr(cluster.coordinator.capacity, "profile_by_id", catalog.get_profile)
    return ProtectedApplication(
        AuthService(UserDAO(cluster.db), SessionDAO(cluster.db)),
        catalog,
        assignments,
        cluster.ledger,
        cluster.coordinator,
    )


@pytest.mark.parametrize("phase", ["capture", "upload"])
def test_slow_staging_allows_listing_and_metadata_but_keeps_data_exclusive(
    cluster, monkeypatch, phase
):
    application = _catalog_application(cluster, monkeypatch)
    entered, release = Event(), Event()
    function = "pack_browser_data" if phase == "capture" else "unpack_browser_data"
    original = getattr(profile_store, function)

    def gated(*args, **kwargs):
        target = args[2] if phase == "capture" else args[1]
        if target.name.startswith("snapshot-" if phase == "capture" else "incoming-"):
            entered.set()
            assert release.wait(5)
        return original(*args, **kwargs)

    if phase == "upload":
        run = launch(cluster)
        cluster.hub.fail_sync = True
        with pytest.raises(DomainError):
            cluster.coordinator.stop(ACTOR, cluster.ref)
    monkeypatch.setattr(profile_store, function, gated)
    with ThreadPoolExecutor(max_workers=2) as pool:
        if phase == "capture":
            work = pool.submit(launch, cluster)
        else:
            work = pool.submit(
                cluster.coordinator.commit_upload,
                run,
                cluster.hub.path.parent / "return.zip",
                cluster.hub.last_upload["archive_digest"],
            )
        try:
            assert entered.wait(3)
            listed = pool.submit(application.list_profiles, ACTOR).result(timeout=1)
            assert [item.profile_id for item in listed] == [cluster.profile.profile_id]
            updated = application.update_profile(
                ACTOR, cluster.profile.profile_id, {"locale": "de-DE"}
            )
            assert updated.locale == "de-DE"
            with pytest.raises(DomainError) as blocked:
                application.update_profile(ACTOR, cluster.profile.profile_id, {"name": "renamed"})
            assert blocked.value.code is ErrorCode.PROFILE_BUSY
            assert (cluster.directory / "cookies.sqlite").read_bytes() == b"old-cookies"
            assert len(cluster.ledger.active_runs()) == 1
        finally:
            release.set()
        result = work.result(timeout=5)
    if phase == "capture":
        assert cluster.hub.last_start_profile["locale"] is None
        assert result.worker_id is not None
    else:
        assert result["revision"] == 2
        assert (cluster.directory / "cookies.sqlite").read_bytes() == b"new-cookie-session"
        assert not cluster.ledger.active_runs()
