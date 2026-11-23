"""Durable browser-data snapshots and canonical commit safety."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import stat
import zipfile
from dataclasses import replace
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
    ArchiveStagingError,
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


def test_journal_is_outside_preexisting_profile_namespace(tmp_path):
    database = ExtensionDatabase(tmp_path / "state.sqlite3")
    data = tmp_path / "canonical"
    profile = Profile(".foxprofile-sync", os_type="linux")
    tenant = data / profile.name
    tenant.mkdir(mode=0o700, parents=True)
    (tenant / "cookies.sqlite").write_bytes(b"existing tenant data")
    store = ProfileStore(database, data, tmp_path / "staging")
    assert store._profile_dir(profile) == tenant
    assert not store._journal_dir.is_relative_to(data)
    assert store._journal_dir.stat().st_dev == data.stat().st_dev
    assert (tenant / "cookies.sqlite").read_bytes() == b"existing tenant data"


def test_referenced_legacy_journal_is_preserved_for_explicit_recovery(tmp_path):
    cluster = _remote_fixture(tmp_path)
    legacy = cluster.data_dir / ".foxprofile-sync"
    legacy.mkdir(mode=0o700, exist_ok=True)
    retained = legacy / "backup-legacy"
    retained.mkdir(mode=0o700)
    (retained / "cookies.sqlite").write_bytes(b"last canonical data")
    with cluster.database.transaction() as db:
        db.execute(
            "INSERT INTO profile_sync_commits "
            "(profile_id, run_id, base_revision, archive_digest, target_revision, receipt_id, "
            "status, backup_name, created_at, updated_at) "
            "VALUES (?, ?, 1, ?, 2, 'legacy-receipt', 'prepared', 'backup-legacy', 1, 1)",
            (cluster.profile.profile_id, cluster.run.ref.run_id, "a" * 64),
        )
    with pytest.raises(ProfileStoreError, match="Legacy profile journal"):
        ProfileStore(cluster.database, cluster.data_dir, cluster.staging)
    assert (retained / "cookies.sqlite").read_bytes() == b"last canonical data"
    with cluster.database.read() as db:
        assert db.execute("SELECT status FROM profile_sync_commits").fetchone()[0] == "prepared"


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


def test_chrome_snapshot_and_return_keep_numbered_leveldb_logs(tmp_path):
    cluster = _remote_fixture(tmp_path, engine="chrome")
    databases = [
        Path("Default/Local Storage/leveldb"),
        Path("Default/IndexedDB/https_example.test_0.indexeddb.leveldb"),
        Path("Default/Session Storage"),
        Path("Profile 2/Service Worker/Database"),
    ]
    for directory in databases:
        source = cluster.profile_dir / directory
        source.mkdir(parents=True)
        (source / "CURRENT").write_bytes(b"MANIFEST-000002\n")
        (source / "MANIFEST-000002").write_bytes(b"manifest")
        (source / "000003.log").write_bytes(b"first write")
        for name in ("LOG", "LOG.old", "LOCK", "debug.log"):
            (source / name).write_bytes(b"diagnostic or lock")
    (cluster.profile_dir / "000004.log").write_bytes(b"unrelated diagnostic")

    snapshot = cluster.store.capture(cluster.profile, cluster.run)
    worker_data = tmp_path / "worker"
    unpack_browser_data(
        snapshot.path, worker_data, snapshot.archive_digest, "chrome", snapshot.identity_digest
    )
    assert not (worker_data / "000004.log").exists()
    for directory in databases:
        files = worker_data / directory
        assert (files / "000003.log").read_bytes() == b"first write"
        assert {p.name for p in files.iterdir()} == {"CURRENT", "MANIFEST-000002", "000003.log"}
        (files / "1000000.log").write_bytes(b"second write")

    returned = tmp_path / "return.zip"
    packed = pack_browser_data(worker_data, "chrome", returned)
    assert packed.identity_digest == snapshot.identity_digest
    run = _advance_to_syncing(cluster)
    receipt = cluster.store.commit(
        cluster.profile, run, returned, snapshot.base_revision, packed.archive_digest
    )
    assert receipt.revision == snapshot.base_revision + 1
    for directory in databases:
        assert (cluster.profile_dir / directory / "1000000.log").read_bytes() == b"second write"


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


@pytest.mark.parametrize("engine", ["camoufox", "chrome"])
def test_master_reconciles_selected_os_before_worker_snapshot(tmp_path, monkeypatch, engine):
    cluster = _remote_fixture(tmp_path, engine=engine)
    profile = replace(cluster.profile, os_type="windows")
    if engine == "camoufox":
        monkeypatch.setattr(
            "camoufox.fingerprints.generate_fingerprint",
            lambda **_: {"navigator": {"platform": "Win32", "userAgent": "fixture-windows"}},
        )
    snapshot = cluster.store.capture(profile, cluster.ledger.get(cluster.run.ref))
    first_identity = snapshot.identity_digest
    unpacked = tmp_path / "worker-copy"
    unpack_browser_data(snapshot.path, unpacked, snapshot.archive_digest, engine, first_identity)
    filename = "fingerprint.json" if engine == "camoufox" else "chrome_persona.json"
    value = json.loads((unpacked / filename).read_text())
    assert value["os" if engine == "camoufox" else "platform"] == "windows"
    assert cluster.store.initialize_identity(profile) == first_identity


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


def test_committed_receipt_replay_validates_exact_run_and_file(tmp_path):
    cluster = _remote_fixture(tmp_path)
    snapshot = cluster.store.capture(cluster.profile, cluster.run)
    run = _advance_to_syncing(cluster)
    replay = cluster.store.replay_committed_receipt
    args = (snapshot.path, snapshot.base_revision, snapshot.archive_digest)
    assert replay(run, *args) is None
    receipt = cluster.store.commit(cluster.profile, run, *args)
    assert replay(run, *args) == receipt
    for stale in (
        replace(run, worker_id="worker-b"),
        replace(run, worker_boot_id="boot-b"),
        replace(run, connection_generation=run.connection_generation + 1),
        replace(run, worker_id=None),
    ):
        with pytest.raises(ProfileStoreError) as error:
            replay(stale, *args)
        assert error.value.code.value == "STALE_RUN"

    with pytest.raises(ProfileStoreError) as wrong_base:
        replay(run, snapshot.path, snapshot.base_revision + 1, snapshot.archive_digest)
    assert wrong_base.value.code.value == "TRANSFER_CONFLICT"
    changed = tmp_path / "changed.zip"
    changed.write_bytes(b"changed bytes")
    with pytest.raises(ProfileStoreError) as changed_digest:
        replay(run, changed, snapshot.base_revision, _sha(changed))
    assert changed_digest.value.code.value == "TRANSFER_CONFLICT"
    with pytest.raises(ProfileStoreError) as changed_bytes:
        replay(run, changed, snapshot.base_revision, snapshot.archive_digest)
    assert changed_bytes.value.code.value == "TRANSFER_INVALID"
    assert replay(run, *args) == receipt
    cluster.store.max_archive_bytes = snapshot.size_bytes - 1
    with pytest.raises(ProfileStoreError) as oversized:
        replay(run, *args)
    assert oversized.value.code.value == "TRANSFER_INVALID"


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
    with pytest.raises(ArchiveStagingError, match="disk space"):
        pack_browser_data(cluster.profile_dir, "camoufox", tmp_path / "no-space.zip")


@pytest.mark.parametrize("disk_error", [errno.ENOSPC, errno.EDQUOT])
def test_packing_disk_exhaustion_preserves_source_for_retry(tmp_path, monkeypatch, disk_error):
    cluster = _remote_fixture(tmp_path)
    target = tmp_path / "retry.zip"
    original = zipfile.ZipFile

    def exhausted(*args, **kwargs):
        raise OSError(disk_error, "fixture archive storage unavailable")

    monkeypatch.setattr(zipfile, "ZipFile", exhausted)
    with pytest.raises(ArchiveStagingError):
        pack_browser_data(cluster.profile_dir, "camoufox", target)
    assert (cluster.profile_dir / "cookies.sqlite").read_bytes() == b"cookie-v1"
    assert not target.exists() and not target.with_suffix(".zip.part").exists()
    monkeypatch.setattr(zipfile, "ZipFile", original)
    assert pack_browser_data(cluster.profile_dir, "camoufox", target).size_bytes > 0


def test_upload_reservations_preserve_floor_across_streams_and_retries(tmp_path, monkeypatch):
    import io

    cluster = _remote_fixture(tmp_path)
    store = cluster.store
    store.staging_reserve_bytes = 100
    free = [250]
    monkeypatch.setattr(
        "foxprofile_ext.workers.profile_store.shutil.disk_usage",
        lambda _: SimpleNamespace(free=free[0]),
    )
    first, second = object(), object()
    store.reserve_upload("run-a", 100, first)
    with pytest.raises(ArchiveStagingError):
        store.reserve_upload("run-b", 100, second)
    with pytest.raises(ProfileStoreError) as duplicate:
        store.reserve_upload("run-a", 1, second)
    assert duplicate.value.code.value == "TRANSFER_CONFLICT"
    store.release_upload("run-a", second)

    class StagingFile(io.BytesIO):
        def write(self, data):
            result = super().write(data)
            free[0] -= result
            return result

    stream = StagingFile()
    store.write_upload("run-a", first, stream, b"a" * 40)
    store.reserve_upload("run-b", 50, second)
    with pytest.raises(ArchiveStagingError):
        store.reserve_upload("run-c", 1, object())
    store.release_upload("run-b", second)
    free[0] = 150  # Another local producer consumed previously available disk.
    with pytest.raises(ArchiveStagingError):
        store.write_upload("run-a", first, stream, b"b")
    assert stream.getvalue() == b"a" * 40
    store.release_upload("run-a", first)
    store.reserve_upload("run-a", 50, second)
    store.release_upload("run-a", second)
    free[0] = 0
    store.reserve_upload("receipt-replay", 0, first)
    store.release_upload("receipt-replay", first)


def test_low_entropy_persistent_data_roundtrips_with_reader_ratio_limit(tmp_path):
    cluster = _remote_fixture(tmp_path)
    # Benign sparse site data, below default archive and expansion limits.
    original = cluster.profile_dir / "site-storage.bin"
    with original.open("wb") as stream:
        stream.truncate(16 * 1024**2)
    archive = tmp_path / "outbound.zip"
    packed = pack_browser_data(cluster.profile_dir, "camoufox", archive)
    worker = tmp_path / "worker-data"
    unpack_browser_data(archive, worker, packed.archive_digest, "camoufox", packed.identity_digest)
    assert (worker / original.name).stat().st_size == original.stat().st_size
    returned = tmp_path / "returned.zip"
    result = pack_browser_data(worker, "camoufox", returned)
    assert result.archive_digest == packed.archive_digest
    master = tmp_path / "restored"
    unpack_browser_data(returned, master, result.archive_digest, "camoufox", result.identity_digest)
    assert (
        hashlib.sha256((master / original.name).read_bytes()).digest()
        == hashlib.sha256(original.read_bytes()).digest()
    )


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
    assert cluster.store._existing_receipt(run.ref.profile_id, run.ref.run_id)["backup_name"]
    monkeypatch.setattr("foxprofile_ext.workers.profile_store.shutil.rmtree", real_rmtree)
    assert cluster.store.recover() == ()
    assert (
        cluster.store._existing_receipt(run.ref.profile_id, run.ref.run_id)["backup_name"] is None
    )


@pytest.mark.parametrize(
    "state",
    [
        "promoted",
        "pending",
        "invalid-stage",
        "missing-stage",
        "corrupt-both",
        "symlink",
        "missing-proof",
    ],
)
def test_recovery_of_prepared_commit_during_filesystem_promotion(tmp_path, state):
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
        if state != "missing-proof":
            db.execute(
                "INSERT INTO profile_archive_contents VALUES (?, ?)",
                (packed.archive_digest, packed.content_digest),
            )
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
    if state == "promoted":
        os.replace(stage, cluster.profile_dir)
    if state in {"invalid-stage", "corrupt-both"}:
        (stage / "cookies.sqlite").write_bytes(b"damaged")
    if state == "corrupt-both":
        (backup / "cookies.sqlite").write_bytes(b"damaged backup")
    if state == "missing-stage":
        shutil.rmtree(stage)
    if state == "symlink":
        cluster.profile_dir.symlink_to(backup, target_is_directory=True)

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
    if state in {"corrupt-both", "symlink", "missing-proof"}:
        with pytest.raises(ProfileStoreError):
            restarted.recover()
        if state == "symlink":
            assert cluster.profile_dir.is_symlink()
            assert (backup / "cookies.sqlite").read_bytes() == b"cookie-v1"
        else:
            assert not cluster.profile_dir.exists()
        assert backup.is_dir()
        assert (
            restarted._existing_receipt(run.ref.profile_id, run.ref.run_id)["status"] == "prepared"
        )
        return

    receipts = restarted.recover()
    if state in {"invalid-stage", "missing-stage"}:
        assert receipts == ()
        assert restarted.revision(run.ref.profile_id)[0] == snapshot.base_revision
        assert restarted._existing_receipt(run.ref.profile_id, run.ref.run_id)["status"] == "failed"
        assert (cluster.profile_dir / "cookies.sqlite").read_bytes() == b"cookie-v1"
    else:
        assert len(receipts) == 1
        assert receipts[0].revision == snapshot.base_revision + 1
        assert receipts[0].receipt_id == receipt_id
        assert (cluster.profile_dir / "cookies.sqlite").read_bytes() == b"recovered-cookie"
        assert (
            restarted.commit(
                cluster.profile, run, upload, snapshot.base_revision, packed.archive_digest
            )
            == receipts[0]
        )
    assert restarted.recover() == ()
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
        assert ledger.mark_running(run.ref, track_local_data=True)
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
