"""Crash windows and durability of the Master's profile commit journal."""

import errno
import hashlib
import os
import shutil
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from foxprofile_ext.errors import DomainError, ErrorCode
from foxprofile_ext.storage.capacity import RunStatus
from foxprofile_ext.workers import profile_store
from foxprofile_ext.workers.http import router
from foxprofile_ext.workers.profile_store import ProfileStore, ProfileStoreError, pack_browser_data
from tests.test_ext_distributed_integration import ACTOR, launch
from tests.test_ext_distributed_integration import cluster as cluster
from tests.test_ext_worker_profile_store import _advance_to_syncing, _remote_fixture


def test_exclusive_recovery_reclaims_abandoned_extractions(tmp_path, monkeypatch):
    fixture = _remote_fixture(tmp_path)
    snapshot = fixture.store.capture(fixture.profile, fixture.ledger.get(fixture.run.ref))
    copy = tmp_path / "copy"
    shutil.copytree(fixture.profile_dir, copy)
    (copy / "cookies.sqlite").write_bytes(b"returned")
    upload = tmp_path / "return.zip"
    packed = pack_browser_data(copy, "camoufox", upload)
    run = _advance_to_syncing(fixture)
    unpack = profile_store.unpack_browser_data

    class Interrupted(BaseException):
        pass

    def crash_before_intent(*args, **kwargs):
        unpack(*args, **kwargs)
        raise Interrupted()

    monkeypatch.setattr(profile_store, "unpack_browser_data", crash_before_intent)
    for _ in range(2):
        with pytest.raises(Interrupted):
            fixture.store.commit(
                fixture.profile, run, upload, snapshot.base_revision, packed.archive_digest
            )
    journal = fixture.store._journal_dir
    orphans = tuple(journal.glob("incoming-*"))
    assert len(orphans) == 2
    assert all((tree / "cookies.sqlite").read_bytes() == b"returned" for tree in orphans)
    unrelated = journal / "incoming-not-a-stage"
    unrelated.mkdir()
    backup = journal / f"backup-{uuid4().hex}"
    backup.mkdir()
    if os.name == "posix":
        link = journal / f"incoming-{uuid4().hex}"
        link.symlink_to(backup, target_is_directory=True)

    restarted = ProfileStore(
        fixture.database, fixture.data_dir, fixture.staging, profile_by_id=lambda _: fixture.profile
    )
    assert restarted.recover() == ()
    assert all(not tree.exists() for tree in orphans)
    assert unrelated.is_dir() and backup.is_dir()
    if os.name == "posix":
        assert link.is_symlink()
    assert (fixture.profile_dir / "cookies.sqlite").read_bytes() == b"cookie-v1"
    monkeypatch.setattr(profile_store, "unpack_browser_data", unpack)
    receipt = restarted.commit(
        fixture.profile, run, upload, snapshot.base_revision, packed.archive_digest
    )
    assert receipt.revision == snapshot.base_revision + 1
    assert (fixture.profile_dir / "cookies.sqlite").read_bytes() == b"returned"


@pytest.mark.skipif(os.name != "posix", reason="POSIX directory descriptor fsync")
@pytest.mark.parametrize("parent", ["journal", "canonical"])
@pytest.mark.parametrize("rename", ["backup", "promotion"])
def test_upload_ack_waits_for_durable_rename_and_restart(cluster, monkeypatch, rename, parent):
    run = launch(cluster)
    cluster.hub.fail_sync = True
    with pytest.raises(DomainError) as failure:
        cluster.coordinator.stop(ACTOR, cluster.ref)
    assert failure.value.code is ErrorCode.SYNC_FAILED
    upload = cluster.hub.last_upload
    archive = (cluster.hub.path.parent / "return.zip").read_bytes()
    headers = {
        **cluster.hub.headers(run, upload["token"]),
        "X-Archive-Digest": hashlib.sha256(archive).hexdigest(),
    }

    fsync = os.fsync
    failed_parent = (
        cluster.directory.parent if parent == "canonical" else cluster.store._journal_dir
    ).stat()
    failures = []

    def fail_directory_sync(fd):
        if os.path.samestat(os.fstat(fd), failed_parent):
            moved = (
                not cluster.directory.exists()
                if rename == "backup"
                else (
                    cluster.directory.is_dir()
                    and (cluster.directory / "cookies.sqlite").read_bytes() == b"new-cookie-session"
                )
            )
            if moved:
                failures.append(rename)
                raise OSError(errno.EIO, "injected directory fsync error")
        return fsync(fd)

    monkeypatch.setattr(os, "fsync", fail_directory_sync)
    app = FastAPI()
    app.state.container = SimpleNamespace(worker_coordinator=cluster.coordinator)
    app.include_router(router, prefix="/api/v1")
    with TestClient(app, base_url="https://master.example.test") as client:
        response = client.post(upload["url"], headers=headers, content=archive)
        assert failures and response.status_code == 503, response.text
        journal = cluster.store._existing_receipt(run.ref.profile_id, run.ref.run_id)
        assert (
            journal["status"] == "prepared" and cluster.store.revision(run.ref.profile_id)[0] == 1
        )
        assert cluster.ledger.get(run.ref).status is RunStatus.SYNCING
        backup = cluster.store._journal_dir / journal["backup_name"]
        assert (backup / "cookies.sqlite").read_bytes() == b"old-cookies"
        changed_tree = (
            cluster.store._journal_dir / journal["stage_name"]
            if rename == "backup"
            else cluster.directory
        )
        assert (changed_tree / "cookies.sqlite").read_bytes() == b"new-cookie-session"

        restarted = ProfileStore(
            cluster.db,
            cluster.directory.parent,
            cluster.store.staging_dir,
            profile_by_id=lambda _: cluster.profile,
        )
        with pytest.raises(ProfileStoreError) as still_unavailable:
            restarted.recover()
        assert still_unavailable.value.code is ErrorCode.SYNC_FAILED
        with pytest.raises(ProfileStoreError) as pending_upload:
            restarted.commit(
                cluster.profile,
                run,
                cluster.hub.path.parent / "return.zip",
                run.base_revision,
                headers["X-Archive-Digest"],
            )
        assert pending_upload.value.code is ErrorCode.SYNC_FAILED
        assert len(failures) == 3
        assert (backup / "cookies.sqlite").read_bytes() == b"old-cookies"
        assert (changed_tree / "cookies.sqlite").read_bytes() == b"new-cookie-session"
        monkeypatch.setattr(os, "fsync", fsync)
        (receipt,) = restarted.recover()
        assert receipt.receipt_id == journal["receipt_id"] and receipt.revision == 2
        assert (cluster.directory / "cookies.sqlite").read_bytes() == b"new-cookie-session"
        assert not backup.exists()
        response = client.post(upload["url"], headers=headers, content=archive)
        assert response.status_code == 200 and response.json()["receipt_id"] == receipt.receipt_id
        assert cluster.ledger.get(run.ref).status is RunStatus.CLOSED


@pytest.mark.skipif(os.name != "posix", reason="POSIX directory descriptor fsync")
def test_nested_extracted_state_is_durable_before_prepared_intent(tmp_path, monkeypatch):
    fixture = _remote_fixture(tmp_path)
    snapshot = fixture.store.capture(fixture.profile, fixture.ledger.get(fixture.run.ref))
    run = _advance_to_syncing(fixture)
    fsync = os.fsync

    def fail_nested_directory(fd):
        for path in fixture.store._journal_dir.glob("incoming-*/storage/default"):
            if os.path.samestat(os.fstat(fd), path.stat()):
                raise OSError(errno.EIO, "nested extraction directory could not be flushed")
        fsync(fd)

    monkeypatch.setattr(os, "fsync", fail_nested_directory)
    with pytest.raises(ProfileStoreError) as failure:
        fixture.store.commit(
            fixture.profile, run, snapshot.path, snapshot.base_revision, snapshot.archive_digest
        )
    assert failure.value.code is ErrorCode.SYNC_FAILED
    assert fixture.store._existing_receipt(run.ref.profile_id, run.ref.run_id) is None
    assert not tuple(fixture.store._journal_dir.glob("incoming-*"))
    assert (fixture.profile_dir / "cookies.sqlite").read_bytes() == b"cookie-v1"
