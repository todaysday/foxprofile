"""Durable browser-data snapshots and canonical commit safety."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from chrome_engine.engine import ChromeEngine
from chrome_engine.persona import Persona
from chrome_engine.persona import save as save_persona
from foxprofile_ext.dto import ProcessIdentity
from foxprofile_ext.errors import DomainError
from foxprofile_ext.storage.capacity import CapacityLedger, ProcessLiveness, RunStatus
from foxprofile_ext.storage.database import ExtensionDatabase
from foxprofile_ext.workers.metrics import NodeMetrics
from foxprofile_ext.workers.placement import PlacementScheduler
from foxprofile_ext.workers.profile_store import (
    ProfileStore,
    ProfileStoreError,
    pack_browser_data,
    unpack_browser_data,
)
from foxprofile_ext.workers.registry import WorkerRegistry
from src.models.profile import Profile

_MANIFEST = "a" * 64
_FINGERPRINT = {
    "os": "linux",
    "fingerprint": {
        "navigator": {"platform": "Linux", "userAgent": "Mozilla/5.0"},
        "screen": {"width": 1366, "height": 768},
    },
}


def _remote_fixture(tmp_path, *, engine="camoufox", clock=lambda: 1000.0):
    database = ExtensionDatabase(tmp_path / "state.sqlite3")
    ledger = CapacityLedger(database)
    workers = WorkerRegistry(database, _MANIFEST, clock=clock)
    credential = workers.enroll("worker-a")
    registered = workers.register("worker-a", credential, "boot-a", _MANIFEST, True)
    workers.heartbeat(
        registered.worker_id,
        "boot-a",
        registered.connection_generation,
        NodeMetrics(16 * 1024**3, None, 32 * 1024**3, (engine,)),
    )
    profile = Profile("alpha", os_type="linux", engine=engine)
    scheduler = PlacementScheduler(
        ledger,
        workers,
        local_metrics=lambda: NodeMetrics(0, None, 32 * 1024**3, ()),
        browser_memory_bytes=1024,
        system_reserve_bytes=0,
        staging_reserve_bytes=0,
        clock=clock,
    )
    admission = scheduler.reserve([profile], "master-local")
    run = ledger.get(admission.newly_reserved[0])
    assert run is not None and run.worker_id == "worker-a"
    assert ledger.transition_remote(
        run.ref,
        run.worker_id,
        run.worker_boot_id,
        run.connection_generation,
        RunStatus.RESERVED,
        RunStatus.TRANSFERRING,
    )
    data_dir = tmp_path / "canonical"
    profile_dir = data_dir / profile.name
    profile_dir.mkdir(parents=True)
    _write_identity(profile_dir, engine)
    _write_browser_state(profile_dir)
    catalog = tmp_path / "profiles.json"
    catalog.write_text(json.dumps({profile.name: profile.to_dict()}), encoding="utf-8")
    staging = tmp_path / "transfer-staging"
    store = ProfileStore(
        database,
        data_dir,
        staging,
        max_archive_bytes=4 * 1024**2,
        max_expanded_bytes=8 * 1024**2,
        max_members=100,
        clock=clock,
        profile_by_id=lambda profile_id: profile if profile_id == profile.profile_id else None,
    )
    return SimpleNamespace(
        database=database,
        ledger=ledger,
        workers=workers,
        profile=profile,
        run=run,
        profile_dir=profile_dir,
        data_dir=data_dir,
        catalog=catalog,
        staging=staging,
        store=store,
        clock=clock,
    )


def _write_identity(profile_dir: Path, engine: str) -> None:
    if engine == "camoufox":
        (profile_dir / "fingerprint.json").write_text(
            json.dumps(_FINGERPRINT, sort_keys=True), encoding="utf-8"
        )
    else:
        save_persona(
            profile_dir,
            Persona(seed=123, platform="linux", platform_version="6.8.0", hardware_concurrency=8),
        )


def _write_browser_state(profile_dir: Path, *, cookies=b"cookie-v1", local=b"local-v1", tabs=None):
    (profile_dir / "cookies.sqlite").write_bytes(cookies)
    storage = profile_dir / "storage" / "default" / "https+++example.test"
    storage.mkdir(parents=True, exist_ok=True)
    (storage / "local-storage.sqlite").write_bytes(local)
    (profile_dir / "tabs.json").write_text(
        json.dumps(tabs or ["https://example.test/home"]), encoding="utf-8"
    )
    # Portable Master metadata and stale launch control files are excluded.
    (profile_dir / "profile.json").write_text('{"name":"stale"}', encoding="utf-8")
    (profile_dir / "DevToolsActivePort").write_text("remote-control", encoding="utf-8")
    (profile_dir / "old.log").write_text("debug output", encoding="utf-8")


def _advance_to_syncing(cluster):
    run = cluster.ledger.get(cluster.run.ref)
    transitions = (
        (RunStatus.TRANSFERRING, RunStatus.STARTING),
        (RunStatus.STARTING, RunStatus.RUNNING),
        (RunStatus.RUNNING, RunStatus.STOPPING),
        (RunStatus.STOPPING, RunStatus.SYNCING),
    )
    for old, new in transitions:
        assert cluster.ledger.transition_remote(
            run.ref,
            run.worker_id,
            run.worker_boot_id,
            run.connection_generation,
            old,
            new,
        )
        run = cluster.ledger.get(run.ref)
    return run


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _bad_archive(path: Path, members, *, mode=None) -> str:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, value in members:
            info = zipfile.ZipInfo(name)
            if mode is not None and name == mode[0]:
                info.external_attr = mode[1] << 16
            archive.writestr(info, value)
    return _sha(path)


def test_snapshot_is_data_only_durable_and_restores_cookies_storage_and_tabs(tmp_path):
    cluster = _remote_fixture(tmp_path)
    run = cluster.ledger.get(cluster.run.ref)
    snapshot = cluster.store.capture(cluster.profile, run)

    assert snapshot.base_revision == 1
    assert snapshot.archive_digest == _sha(snapshot.path)
    assert cluster.store.get_snapshot(run.ref.run_id) == snapshot
    assert cluster.ledger.get(run.ref).base_revision == snapshot.base_revision

    worker_data = tmp_path / "worker-private" / "profile"
    unpacked = unpack_browser_data(
        snapshot.path,
        worker_data,
        snapshot.archive_digest,
        cluster.profile.engine,
        expected_identity=snapshot.identity_digest,
        max_archive_bytes=cluster.store.max_archive_bytes,
        max_expanded_bytes=cluster.store.max_expanded_bytes,
        max_members=cluster.store.max_members,
    )
    assert unpacked.identity_digest == snapshot.identity_digest
    assert (worker_data / "cookies.sqlite").read_bytes() == b"cookie-v1"
    assert (
        worker_data / "storage/default/https+++example.test/local-storage.sqlite"
    ).read_bytes() == b"local-v1"
    assert json.loads((worker_data / "tabs.json").read_text()) == ["https://example.test/home"]
    assert not (worker_data / "profile.json").exists()
    assert not (worker_data / "DevToolsActivePort").exists()
    assert not (worker_data / "old.log").exists()
    assert cluster.catalog.read_text() == json.dumps(
        {cluster.profile.name: cluster.profile.to_dict()}
    )


@pytest.mark.parametrize("engine", ["camoufox", "chrome"])
def test_identity_initialization_preserves_existing_engine_identity(tmp_path, monkeypatch, engine):
    cluster = _remote_fixture(tmp_path, engine=engine)
    identity_path = cluster.profile_dir / (
        "fingerprint.json" if engine == "camoufox" else "chrome_persona.json"
    )
    before = identity_path.read_bytes()

    if engine == "camoufox":
        monkeypatch.setattr(
            "src.services.browser.fingerprint.load_or_create",
            lambda *_args, **_kwargs: pytest.fail("existing identity must not regenerate"),
        )
    else:
        monkeypatch.setattr(
            "chrome_engine.persona.load_or_create",
            lambda *_args, **_kwargs: pytest.fail("existing identity must not regenerate"),
        )

    digest = cluster.store.initialize_identity(cluster.profile)
    assert identity_path.read_bytes() == before
    assert digest


def test_master_initializes_missing_identity_once_but_rejects_existing_bad_identity(
    tmp_path, monkeypatch
):
    cluster = _remote_fixture(tmp_path)
    identity = cluster.profile_dir / "fingerprint.json"
    identity.unlink()
    generated = []

    def generate(path, os_type, **overrides):
        generated.append((path, os_type, overrides))
        identity.write_text(json.dumps(_FINGERPRINT), encoding="utf-8")
        return _FINGERPRINT["fingerprint"]

    monkeypatch.setattr("src.services.browser.fingerprint.load_or_create", generate)
    first = cluster.store.initialize_identity(cluster.profile)
    second = cluster.store.initialize_identity(cluster.profile)
    assert first == second and len(generated) == 1

    identity.write_text('{"os":"windows","fingerprint":{}}', encoding="utf-8")
    with pytest.raises(ProfileStoreError):
        cluster.store.initialize_identity(cluster.profile)
    assert len(generated) == 1


def test_scoped_grants_reject_wrong_owner_expiry_and_replay_but_allow_retries(tmp_path):
    now = [1000.0]
    cluster = _remote_fixture(tmp_path, clock=lambda: now[0])
    run = cluster.ledger.get(cluster.run.ref)
    snapshot = cluster.store.capture(cluster.profile, run)
    grant = cluster.store.issue_grant(run, "download", snapshot=snapshot, ttl=2)
    assert grant.max_archive_bytes == cluster.store.max_archive_bytes

    with cluster.database.read() as db:
        row = db.execute("SELECT token_hash FROM profile_sync_grants").fetchone()
    assert row["token_hash"] != grant.token
    with pytest.raises(DomainError) as wrong_worker:
        cluster.store.validate_grant(
            grant.token,
            operation="download",
            profile_id=run.ref.profile_id,
            run_id=run.ref.run_id,
            worker_id="worker-b",
            worker_boot_id=run.worker_boot_id,
            connection_generation=run.connection_generation,
            consume=False,
        )
    assert wrong_worker.value.code.value == "TRANSFER_INVALID"

    # Download grants remain usable for a restarted/interrupted stream.
    for _ in range(2):
        validated = cluster.store.validate_grant(
            grant.token,
            operation="download",
            profile_id=run.ref.profile_id,
            run_id=run.ref.run_id,
            worker_id=run.worker_id,
            worker_boot_id=run.worker_boot_id,
            connection_generation=run.connection_generation,
            consume=True,
        )
        assert validated.archive_digest == snapshot.archive_digest
    now[0] += 3
    with pytest.raises(DomainError) as expired:
        cluster.store.validate_grant(
            grant.token,
            operation="download",
            profile_id=run.ref.profile_id,
            run_id=run.ref.run_id,
            worker_id=run.worker_id,
            worker_boot_id=run.worker_boot_id,
            connection_generation=run.connection_generation,
            consume=False,
        )
    assert expired.value.code.value == "TRANSFER_INVALID"


def test_commit_persists_browser_changes_once_and_ack_retry_survives_closed_run(tmp_path):
    cluster = _remote_fixture(tmp_path)
    run = cluster.ledger.get(cluster.run.ref)
    snapshot = cluster.store.capture(cluster.profile, run)
    worker_data = tmp_path / "worker-data" / "profile"
    unpack_browser_data(
        snapshot.path, worker_data, snapshot.archive_digest, "camoufox", snapshot.identity_digest
    )
    _write_browser_state(
        worker_data,
        cookies=b"cookie-v2",
        local=b"local-v2",
        tabs=["https://example.test/new"],
    )
    # Worker changed only persistent browser data; source profile.json remains unavailable.
    worker_archive = tmp_path / "worker-upload.zip"
    packed = pack_browser_data(worker_data, "camoufox", worker_archive)
    run = _advance_to_syncing(cluster)
    grant = cluster.store.issue_grant(
        run,
        "upload",
        archive_digest=packed.archive_digest,
        base_revision=run.base_revision,
    )
    validated = cluster.store.validate_grant(
        grant.token,
        operation="upload",
        profile_id=run.ref.profile_id,
        run_id=run.ref.run_id,
        worker_id=run.worker_id,
        worker_boot_id=run.worker_boot_id,
        connection_generation=run.connection_generation,
        consume=False,
    )
    assert validated.archive_digest == packed.archive_digest
    cluster.store.validate_grant(
        grant.token,
        operation="upload",
        profile_id=run.ref.profile_id,
        run_id=run.ref.run_id,
        worker_id=run.worker_id,
        worker_boot_id=run.worker_boot_id,
        connection_generation=run.connection_generation,
        consume=True,
    )
    # Issuing another grant must not erase the consumed token before the HTTP
    # response is known to have reached the Worker.
    cluster.store.issue_grant(
        run,
        "upload",
        archive_digest=packed.archive_digest,
        base_revision=run.base_revision,
    )

    receipt = cluster.store.commit(
        cluster.profile,
        run,
        worker_archive,
        snapshot.base_revision,
        packed.archive_digest,
    )
    assert receipt.revision == snapshot.base_revision + 1
    assert (cluster.profile_dir / "cookies.sqlite").read_bytes() == b"cookie-v2"
    assert (
        cluster.profile_dir / "storage/default/https+++example.test/local-storage.sqlite"
    ).read_bytes() == b"local-v2"
    assert json.loads((cluster.profile_dir / "tabs.json").read_text()) == [
        "https://example.test/new"
    ]
    assert cluster.catalog.read_text() == json.dumps(
        {cluster.profile.name: cluster.profile.to_dict()}
    )

    assert cluster.ledger.complete_remote(
        run.ref,
        run.worker_id,
        run.worker_boot_id,
        run.connection_generation,
        process_stopped=True,
        sync_verified=True,
    )
    closed = cluster.ledger.get(run.ref)
    assert closed.status is RunStatus.CLOSED
    retry_grant = cluster.store.validate_grant(
        grant.token,
        operation="upload",
        profile_id=run.ref.profile_id,
        run_id=run.ref.run_id,
        worker_id=run.worker_id,
        worker_boot_id=run.worker_boot_id,
        connection_generation=run.connection_generation,
        consume=False,
    )
    assert retry_grant.archive_digest == packed.archive_digest
    retried = cluster.store.commit(
        cluster.profile,
        closed,
        worker_archive,
        snapshot.base_revision,
        packed.archive_digest,
    )
    assert retried == receipt
    assert cluster.store.revision(cluster.profile.profile_id)[0] == receipt.revision

    orphan_part = cluster.staging / f"{snapshot.path.name}.part"
    orphan_part.write_bytes(b"interrupted archive write")
    cluster.store.cleanup_run(run.ref.run_id)
    assert cluster.store.get_snapshot(run.ref.run_id) is None
    assert not snapshot.path.exists()
    assert not orphan_part.exists()
    retry_grant = cluster.store.validate_grant(
        grant.token,
        operation="upload",
        profile_id=run.ref.profile_id,
        run_id=run.ref.run_id,
        worker_id=run.worker_id,
        worker_boot_id=run.worker_boot_id,
        connection_generation=run.connection_generation,
        consume=False,
    )
    assert retry_grant.archive_digest == packed.archive_digest
    (worker_data / "cookies.sqlite").write_bytes(b"different-retry")
    changed_upload = tmp_path / "different-retry.zip"
    changed = pack_browser_data(worker_data, "camoufox", changed_upload)
    with pytest.raises(DomainError) as changed_retry:
        cluster.store.commit(
            cluster.profile,
            closed,
            changed_upload,
            snapshot.base_revision,
            changed.archive_digest,
        )
    assert changed_retry.value.code.value == "TRANSFER_CONFLICT"
    assert (
        cluster.store.commit(
            cluster.profile,
            closed,
            worker_archive,
            snapshot.base_revision,
            packed.archive_digest,
        )
        == receipt
    )


def test_cleanup_refuses_live_run_and_preserves_snapshot(tmp_path):
    cluster = _remote_fixture(tmp_path)
    run = cluster.ledger.get(cluster.run.ref)
    snapshot = cluster.store.capture(cluster.profile, run)
    with pytest.raises(DomainError) as active:
        cluster.store.cleanup_run(run.ref.run_id)
    assert active.value.code.value == "STALE_RUN"
    assert snapshot.path.exists()


def test_cleanup_quarantined_run_removes_snapshot_and_all_grants(tmp_path):
    cluster = _remote_fixture(tmp_path)
    run = cluster.ledger.get(cluster.run.ref)
    snapshot = cluster.store.capture(cluster.profile, run)
    cluster.store.issue_grant(run, "download", snapshot=snapshot)
    run = _advance_to_syncing(cluster)
    cluster.store.issue_grant(
        run,
        "upload",
        archive_digest=snapshot.archive_digest,
        base_revision=snapshot.base_revision,
    )
    assert cluster.ledger.complete_remote(
        run.ref,
        run.worker_id,
        run.worker_boot_id,
        run.connection_generation,
        process_stopped=True,
        quarantined=True,
    )

    cluster.store.cleanup_run(run.ref.run_id)

    assert cluster.store.get_snapshot(run.ref.run_id) is None
    with cluster.database.read() as db:
        grants = db.execute(
            "SELECT COUNT(*) FROM profile_sync_grants WHERE run_id = ?", (run.ref.run_id,)
        ).fetchone()[0]
    assert grants == 0
    assert (cluster.profile_dir / "cookies.sqlite").read_bytes() == b"cookie-v1"


def test_commit_rejects_changed_identity_wrong_base_and_non_syncing_run(tmp_path):
    cluster = _remote_fixture(tmp_path)
    run = cluster.ledger.get(cluster.run.ref)
    snapshot = cluster.store.capture(cluster.profile, run)
    worker_data = tmp_path / "worker-data" / "profile"
    unpack_browser_data(
        snapshot.path, worker_data, snapshot.archive_digest, "camoufox", snapshot.identity_digest
    )
    (worker_data / "fingerprint.json").write_text(
        json.dumps({"os": "linux", "fingerprint": {"navigator": {"platform": "changed"}}}),
        encoding="utf-8",
    )
    bad_upload = tmp_path / "changed-identity.zip"
    bad_packed = pack_browser_data(worker_data, "camoufox", bad_upload)
    run = _advance_to_syncing(cluster)
    with pytest.raises(DomainError) as changed_identity:
        cluster.store.commit(
            cluster.profile,
            run,
            bad_upload,
            snapshot.base_revision,
            bad_packed.archive_digest,
        )
    assert changed_identity.value.code.value == "TRANSFER_INVALID"
    assert (cluster.profile_dir / "cookies.sqlite").read_bytes() == b"cookie-v1"
    assert cluster.store.revision(cluster.profile.profile_id)[0] == snapshot.base_revision

    valid_data = tmp_path / "valid-worker" / "profile"
    unpack_browser_data(
        snapshot.path, valid_data, snapshot.archive_digest, "camoufox", snapshot.identity_digest
    )
    valid_upload = tmp_path / "valid-upload.zip"
    packed = pack_browser_data(valid_data, "camoufox", valid_upload)
    with pytest.raises(DomainError) as wrong_base:
        cluster.store.commit(
            cluster.profile, run, valid_upload, snapshot.base_revision + 1, packed.archive_digest
        )
    assert wrong_base.value.code.value == "TRANSFER_CONFLICT"

    wrong_phase_cluster = _remote_fixture(tmp_path / "wrong-phase")
    wrong_phase_run = wrong_phase_cluster.ledger.get(wrong_phase_cluster.run.ref)
    assert wrong_phase_cluster.ledger.transition_remote(
        wrong_phase_run.ref,
        wrong_phase_run.worker_id,
        wrong_phase_run.worker_boot_id,
        wrong_phase_run.connection_generation,
        RunStatus.TRANSFERRING,
        RunStatus.STARTING,
    )
    with pytest.raises(DomainError) as wrong_phase:
        wrong_phase_cluster.store.capture(
            wrong_phase_cluster.profile,
            wrong_phase_cluster.ledger.get(wrong_phase_cluster.run.ref),
        )
    assert wrong_phase.value.code.value == "STALE_RUN"


def test_profile_source_rejects_symlinks_and_special_files(tmp_path):
    cluster = _remote_fixture(tmp_path)
    link = cluster.profile_dir / "linked-cookie"
    link.symlink_to(cluster.profile_dir / "cookies.sqlite")
    with pytest.raises(ProfileStoreError, match="symbolic link"):
        pack_browser_data(cluster.profile_dir, "camoufox", tmp_path / "symlink.zip")
    link.unlink()

    if hasattr(os, "mkfifo"):
        fifo = cluster.profile_dir / "browser-pipe"
        os.mkfifo(fifo)
        with pytest.raises(ProfileStoreError, match="special file"):
            pack_browser_data(cluster.profile_dir, "camoufox", tmp_path / "fifo.zip")


@pytest.mark.parametrize(
    "members,mode,expected",
    [
        (
            [
                ("data/../escape", b"x"),
                ("data/fingerprint.json", json.dumps(_FINGERPRINT).encode()),
            ],
            None,
            "path",
        ),
        (
            [
                ("data/fingerprint.json", json.dumps(_FINGERPRINT).encode()),
                ("data/fingerprint.json", b"duplicate"),
            ],
            None,
            "duplicate",
        ),
        (
            [
                ("data/fingerprint.json", json.dumps(_FINGERPRINT).encode()),
                ("data/linked", b"target"),
            ],
            ("data/linked", stat.S_IFLNK | 0o777),
            "link",
        ),
    ],
)
def test_unpack_rejects_traversal_duplicate_and_link_members_before_writing(
    tmp_path, members, mode, expected
):
    archive = tmp_path / "unsafe.zip"
    digest = _bad_archive(archive, members, mode=mode)
    destination = tmp_path / "stage" / "profile"
    with pytest.raises(ProfileStoreError):
        unpack_browser_data(archive, destination, digest, "camoufox")
    assert not destination.exists()


def test_archive_digest_member_size_expanded_limit_and_disk_preflight(tmp_path, monkeypatch):
    cluster = _remote_fixture(tmp_path)
    corrupted = cluster.staging / "corrupt.zip"
    packed = pack_browser_data(cluster.profile_dir, "camoufox", corrupted)
    corrupted.write_bytes(corrupted.read_bytes() + b"tamper")
    with pytest.raises(ProfileStoreError, match="digest"):
        unpack_browser_data(
            corrupted, tmp_path / "corrupt-stage", packed.archive_digest, "camoufox"
        )

    too_few = tmp_path / "too-many.zip"
    with pytest.raises(ProfileStoreError, match="members"):
        pack_browser_data(cluster.profile_dir, "camoufox", too_few, max_members=1)

    oversized = tmp_path / "oversized.zip"
    with pytest.raises(ProfileStoreError, match="limits"):
        pack_browser_data(cluster.profile_dir, "camoufox", oversized, max_expanded_bytes=16)

    monkeypatch.setattr(
        "foxprofile_ext.workers.profile_store.shutil.disk_usage",
        lambda _path: SimpleNamespace(free=0),
    )
    with pytest.raises(ProfileStoreError, match="disk space"):
        pack_browser_data(cluster.profile_dir, "camoufox", tmp_path / "no-space.zip")


def test_clean_commit_failure_restores_last_known_good_data(tmp_path, monkeypatch):
    cluster = _remote_fixture(tmp_path)
    initial = cluster.ledger.get(cluster.run.ref)
    snapshot = cluster.store.capture(cluster.profile, initial)
    worker_data = tmp_path / "worker-data" / "profile"
    unpack_browser_data(
        snapshot.path, worker_data, snapshot.archive_digest, "camoufox", snapshot.identity_digest
    )
    (worker_data / "cookies.sqlite").write_bytes(b"uncommitted-cookie")
    upload = tmp_path / "upload.zip"
    packed = pack_browser_data(worker_data, "camoufox", upload)
    run = _advance_to_syncing(cluster)

    promote = cluster.store._promote

    def promote_then_fail(*args, **kwargs):
        promote(*args, **kwargs)
        raise OSError("injected post-promotion failure")

    monkeypatch.setattr(cluster.store, "_promote", promote_then_fail)
    with pytest.raises(DomainError) as error:
        cluster.store.commit(
            cluster.profile, run, upload, snapshot.base_revision, packed.archive_digest
        )
    assert error.value.code.value == "SYNC_FAILED"
    assert (cluster.profile_dir / "cookies.sqlite").read_bytes() == b"cookie-v1"
    assert cluster.store.revision(cluster.profile.profile_id)[0] == snapshot.base_revision


def test_backup_cleanup_failure_does_not_roll_back_a_committed_revision(tmp_path, monkeypatch):
    cluster = _remote_fixture(tmp_path)
    run = cluster.ledger.get(cluster.run.ref)
    snapshot = cluster.store.capture(cluster.profile, run)
    worker_data = tmp_path / "worker-data" / "profile"
    unpack_browser_data(
        snapshot.path, worker_data, snapshot.archive_digest, "camoufox", snapshot.identity_digest
    )
    (worker_data / "cookies.sqlite").write_bytes(b"committed-cookie")
    upload = tmp_path / "upload.zip"
    packed = pack_browser_data(worker_data, "camoufox", upload)
    run = _advance_to_syncing(cluster)
    real_rmtree = shutil.rmtree

    def fail_backup_cleanup(path, *args, **kwargs):
        if Path(path).name.startswith("backup-"):
            raise OSError("injected cleanup failure")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr("foxprofile_ext.workers.profile_store.shutil.rmtree", fail_backup_cleanup)
    receipt = cluster.store.commit(
        cluster.profile, run, upload, snapshot.base_revision, packed.archive_digest
    )

    assert receipt.revision == snapshot.base_revision + 1
    assert (cluster.profile_dir / "cookies.sqlite").read_bytes() == b"committed-cookie"
    assert cluster.store.revision(cluster.profile.profile_id)[0] == receipt.revision


def test_recovery_finishes_prepared_commit_after_filesystem_promotion(tmp_path):
    cluster = _remote_fixture(tmp_path)
    initial = cluster.ledger.get(cluster.run.ref)
    snapshot = cluster.store.capture(cluster.profile, initial)
    worker_data = tmp_path / "worker-data" / "profile"
    unpack_browser_data(
        snapshot.path, worker_data, snapshot.archive_digest, "camoufox", snapshot.identity_digest
    )
    (worker_data / "cookies.sqlite").write_bytes(b"recovered-cookie")
    upload = tmp_path / "upload.zip"
    packed = pack_browser_data(worker_data, "camoufox", upload)
    run = _advance_to_syncing(cluster)

    stage_name, backup_name, receipt_id = "incoming-crash", "backup-crash", "receipt-crash"
    stage = cluster.store._journal_dir / stage_name
    unpack_browser_data(upload, stage, packed.archive_digest, "camoufox", snapshot.identity_digest)
    with cluster.database.transaction() as db:
        db.execute(
            "INSERT INTO profile_sync_commits "
            "(profile_id, run_id, worker_id, worker_boot_id, connection_generation, base_revision, "
            "archive_digest, target_revision, receipt_id, status, stage_name, backup_name, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'prepared', ?, ?, ?, ?)",
            (
                run.ref.profile_id,
                run.ref.run_id,
                run.worker_id,
                run.worker_boot_id,
                run.connection_generation,
                snapshot.base_revision,
                packed.archive_digest,
                snapshot.base_revision + 1,
                receipt_id,
                stage_name,
                backup_name,
                1000.0,
                1000.0,
            ),
        )
    backup = cluster.store._journal_dir / backup_name
    os.replace(cluster.profile_dir, backup)
    os.replace(stage, cluster.profile_dir)

    restarted = ProfileStore(
        cluster.database,
        cluster.data_dir,
        cluster.staging,
        max_archive_bytes=cluster.store.max_archive_bytes,
        max_expanded_bytes=cluster.store.max_expanded_bytes,
        max_members=cluster.store.max_members,
        clock=cluster.clock,
        profile_by_id=lambda profile_id: (
            cluster.profile if profile_id == cluster.profile.profile_id else None
        ),
    )
    receipts = restarted.recover()
    assert len(receipts) == 1
    assert receipts[0].revision == snapshot.base_revision + 1
    assert receipts[0].receipt_id == receipt_id
    assert (cluster.profile_dir / "cookies.sqlite").read_bytes() == b"recovered-cookie"
    assert not backup.exists()


def test_capture_adopts_valid_master_identity_reset_as_a_new_canonical_revision(tmp_path):
    cluster = _remote_fixture(tmp_path)
    first_run = cluster.ledger.get(cluster.run.ref)
    first = cluster.store.capture(cluster.profile, first_run)
    changed = dict(_FINGERPRINT)
    changed["fingerprint"] = {"navigator": {"platform": "Linux alternate"}}
    (cluster.profile_dir / "fingerprint.json").write_text(json.dumps(changed), encoding="utf-8")

    run = _advance_to_syncing(cluster)
    assert cluster.ledger.complete_remote(
        run.ref,
        run.worker_id,
        run.worker_boot_id,
        run.connection_generation,
        process_stopped=True,
        quarantined=True,
    )
    # Reuse the real placement scheduler against the same exact profile.
    scheduler = PlacementScheduler(
        cluster.ledger,
        cluster.workers,
        local_metrics=lambda: NodeMetrics(0, None, 32 * 1024**3, ()),
        browser_memory_bytes=1024,
        system_reserve_bytes=0,
        staging_reserve_bytes=0,
        clock=cluster.clock,
    )
    second_ref = scheduler.reserve([cluster.profile], "master-local").newly_reserved[0]
    assert cluster.ledger.transition_remote(
        second_ref,
        "worker-a",
        "boot-a",
        cluster.ledger.get(second_ref).connection_generation,
        RunStatus.RESERVED,
        RunStatus.TRANSFERRING,
    )
    second = cluster.store.capture(cluster.profile, cluster.ledger.get(second_ref))
    assert second.base_revision == first.base_revision + 1
    assert second.identity_digest != first.identity_digest


def test_confirmed_local_stops_bookkeep_browser_data_and_identity_revisions(tmp_path):
    database = ExtensionDatabase(tmp_path / "local.sqlite3")
    ledger = CapacityLedger(database)
    profile = Profile("local", os_type="linux")
    data_dir = tmp_path / "canonical"
    profile_dir = data_dir / profile.name
    profile_dir.mkdir(parents=True)
    _write_identity(profile_dir, "camoufox")
    _write_browser_state(profile_dir)
    store = ProfileStore(database, data_dir, tmp_path / "staging")

    def stopped_run(process_id):
        run = ledger.reserve([profile.profile_id], "master-local").runs[0]
        process = ProcessIdentity(process_id, 1000 + process_id)
        assert ledger.claim(run.ref, "master-local", process)
        assert ledger.mark_running(run.ref)
        assert ledger.mark_stopping(run.ref)
        assert ledger.release_after_exit(
            run.ref,
            process,
            lambda _host, _process: ProcessLiveness.DEAD,
        )
        return ledger.get(run.ref)

    first = store.record_local_stop(profile, stopped_run(55))
    (profile_dir / "cookies.sqlite").write_bytes(b"cookie-after-local-run")
    changed = {"os": "linux", "fingerprint": {"navigator": {"platform": "reset"}}}
    (profile_dir / "fingerprint.json").write_text(json.dumps(changed), encoding="utf-8")
    second = store.record_local_stop(profile, stopped_run(56))
    assert first.revision == 1
    assert second.revision == 2
    assert store.revision(profile.profile_id)[0] == 2


def test_chrome_profile_overrides_are_normalized_without_changing_device_identity(tmp_path):
    cluster = _remote_fixture(tmp_path, engine="chrome")
    cluster.profile.timezone = "Europe/London"
    cluster.profile.locale = "en-GB"
    identity_path = cluster.profile_dir / "chrome_persona.json"
    original = Persona.from_json(json.loads(identity_path.read_text()))
    snapshot = cluster.store.capture(cluster.profile, cluster.ledger.get(cluster.run.ref))
    normalized = Persona.from_json(json.loads(identity_path.read_text()))
    assert normalized.seed == original.seed
    assert normalized.platform == original.platform
    assert normalized.timezone == "Europe/London"
    assert normalized.locale == "en-GB"

    worker_data = tmp_path / "worker-data" / "profile"
    unpack_browser_data(
        snapshot.path,
        worker_data,
        snapshot.archive_digest,
        "chrome",
        snapshot.identity_digest,
    )
    worker_identity = worker_data / "chrome_persona.json"
    before_worker_launch = worker_identity.read_bytes()
    engine = ChromeEngine(
        worker_data,
        platform="linux",
        timezone=cluster.profile.timezone,
        locale=cluster.profile.locale,
        geoip=False,
    )
    persona = engine._resolve_persona()
    assert persona.timezone == "Europe/London"
    assert persona.locale == "en-GB"
    assert worker_identity.read_bytes() == before_worker_launch

    (worker_data / "cookies.sqlite").write_bytes(b"chrome-cookie-v2")
    upload = tmp_path / "chrome-upload.zip"
    packed = pack_browser_data(worker_data, "chrome", upload)
    run = _advance_to_syncing(cluster)
    receipt = cluster.store.commit(
        cluster.profile, run, upload, snapshot.base_revision, packed.archive_digest
    )
    assert receipt.revision == snapshot.base_revision + 1
    assert identity_path.read_bytes() == before_worker_launch
    assert (cluster.profile_dir / "cookies.sqlite").read_bytes() == b"chrome-cookie-v2"


def test_chrome_auto_geo_overrides_clear_old_persona_values_before_snapshot(tmp_path):
    cluster = _remote_fixture(tmp_path, engine="chrome")
    save_persona(
        cluster.profile_dir,
        Persona(
            seed=456,
            platform="linux",
            platform_version="6.8.0",
            hardware_concurrency=8,
            timezone="Europe/London",
            locale="en-GB",
        ),
    )
    snapshot = cluster.store.capture(cluster.profile, cluster.ledger.get(cluster.run.ref))
    worker_data = tmp_path / "worker-auto" / "profile"
    unpack_browser_data(
        snapshot.path,
        worker_data,
        snapshot.archive_digest,
        "chrome",
        snapshot.identity_digest,
    )
    identity = worker_data / "chrome_persona.json"
    before_worker_launch = identity.read_bytes()
    engine = ChromeEngine(
        worker_data,
        platform="linux",
        timezone="",
        locale="",
        geoip=False,
    )
    persona = engine._resolve_persona()
    assert persona.timezone is None
    assert persona.locale is None
    assert identity.read_bytes() == before_worker_launch


def test_master_metadata_edit_after_snapshot_does_not_invalidate_current_run(tmp_path):
    cluster = _remote_fixture(tmp_path, engine="chrome")
    run = cluster.ledger.get(cluster.run.ref)
    snapshot = cluster.store.capture(cluster.profile, run)
    cluster.profile.timezone = "Europe/London"
    cluster.profile.locale = "en-GB"
    cluster.catalog.write_text(
        json.dumps({cluster.profile.name: cluster.profile.to_dict()}), encoding="utf-8"
    )

    worker_data = tmp_path / "worker-data" / "profile"
    unpack_browser_data(
        snapshot.path,
        worker_data,
        snapshot.archive_digest,
        "chrome",
        snapshot.identity_digest,
    )
    engine = ChromeEngine(
        worker_data,
        platform="linux",
        timezone="",
        locale="",
        geoip=False,
    )
    engine._resolve_persona()
    (worker_data / "cookies.sqlite").write_bytes(b"current-run-cookie")
    upload = tmp_path / "current-run-upload.zip"
    packed = pack_browser_data(worker_data, "chrome", upload)
    syncing = _advance_to_syncing(cluster)
    receipt = cluster.store.commit(
        cluster.profile, syncing, upload, snapshot.base_revision, packed.archive_digest
    )

    assert receipt.revision == snapshot.base_revision + 1
    saved = Persona.from_json(json.loads((cluster.profile_dir / "chrome_persona.json").read_text()))
    assert saved.timezone is None and saved.locale is None
    assert cluster.profile.timezone == "Europe/London"
    assert cluster.profile.locale == "en-GB"
    saved_metadata = json.loads(cluster.catalog.read_text())[cluster.profile.name]
    assert saved_metadata["timezone"] == "Europe/London"
    assert saved_metadata["locale"] == "en-GB"
