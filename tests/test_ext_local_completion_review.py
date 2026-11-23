"""Local revision ownership and explicit storage outcomes."""

import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from foxprofile_ext.dto import ProcessIdentity
from foxprofile_ext.locking import catalog_lock
from foxprofile_ext.runtime.processes import RuntimeSupervisor
from foxprofile_ext.storage.capacity import CapacityLedger, ProcessLiveness, RunStatus
from foxprofile_ext.storage.database import ExtensionDatabase
from foxprofile_ext.workers.profile_store import ProfileStore
from src.models.profile import Profile
from tests.test_ext_distributed_capacity import _remote_run
from tests.test_ext_supervisor import ROOT, line, start_supervisor, stop_owned
from tests.test_ext_worker_profile_store import _remote_fixture

_BROWSER_PROTOCOL = """
import json, os, pathlib, signal, sys
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
print("RUNTIME:" + json.dumps(dict(display=None, namespace=os.readlink("/proc/self/ns/pid"), tmp_path="/tmp", xauthority_path=None)), flush=True)
print("CONTROL:9001:fixture-run-token", flush=True)
print("BROWSER_STARTED", flush=True)
"""


@pytest.mark.parametrize("kind", ["helper", "failed_start"])
def test_local_non_session_preserves_prior_remote_loss(tmp_path, kind):
    ledger = CapacityLedger(ExtensionDatabase(tmp_path / "state.sqlite3"))
    lost = _remote_run(ledger.database, status="uncertain")
    assert ledger.complete_remote(
        lost, "worker-a", "boot-a", 4, process_stopped=True, quarantined=True
    )
    ref = ledger.reserve([lost.profile_id], "master").newly_reserved[0]
    identity = ProcessIdentity(901, 17)
    assert ledger.claim(ref, "master", identity)
    if kind == "helper":
        assert ledger.mark_stopping(ref)
    assert ledger.release_after_exit(ref, identity, lambda *_: ProcessLiveness.DEAD)
    assert ledger.lifecycle_snapshot()[lost.profile_id] == ("unsaved", "remote")


def test_record_local_stop_ignores_transfer_limits_and_replays_receipt(tmp_path, monkeypatch):
    database = ExtensionDatabase(tmp_path / "state.sqlite3")
    ledger = CapacityLedger(database)
    profile = Profile("local", os_type="linux")
    root = tmp_path / "data" / profile.name
    root.mkdir(parents=True)
    (root / "fingerprint.json").write_text('{"os":"linux","fingerprint":{"fixture":1}}')
    (root / "cookies.sqlite").write_bytes(b"local" * 4000)
    store = ProfileStore(
        database,
        root.parent,
        tmp_path / "staging",
        max_members=1,
        max_expanded_bytes=4,
        max_archive_bytes=4,
    )
    run = ledger.reserve([profile.profile_id], "master").runs[0]
    identity = ProcessIdentity(902, 18)
    assert ledger.claim(run.ref, "master", identity)
    assert ledger.mark_running(run.ref, track_local_data=True)
    assert ledger.release_after_exit(run.ref, identity, lambda *_: ProcessLiveness.DEAD)
    monkeypatch.setattr(
        "foxprofile_ext.workers.profile_store.shutil.disk_usage", lambda _: SimpleNamespace(free=0)
    )
    receipt = store.record_local_stop(profile, run)
    assert receipt.revision == 1
    assert list(store.staging_dir.iterdir()) == []
    ledger.reserve([profile.profile_id], "master")
    (root / "cookies.sqlite").write_bytes(b"next session")
    assert store.record_local_stop(profile, run) == receipt


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux supervisor protocol")
@pytest.mark.parametrize("mode", ["natural", "stop", "shutdown", "crash", "invalid_identity"])
def test_trusted_local_supervisor_owns_completion(tmp_path, mode):
    code = _BROWSER_PROTOCOL
    if mode == "invalid_identity":
        code += f"pathlib.Path({str(tmp_path / 'data/alpha/fingerprint.json')!r}).write_bytes(b'broken')\n"
    finish = {
        "natural": "print('BROWSER_CLOSED',flush=True)",
        "stop": "signal.pause()",
        "shutdown": "signal.pause()",
        "crash": "sys.exit(17)",
        "invalid_identity": "print('BROWSER_CLOSED',flush=True)",
    }
    database, ledger, ref, process, _ = start_supervisor(
        tmp_path, code + finish[mode], local_store=True
    )
    runtime = RuntimeSupervisor(
        database, data_dir=tmp_path / "data", code_root=ROOT, runtime_paths=()
    )
    try:
        assert line(process).startswith("CONTROL:")
        assert line(process) == "BROWSER_STARTED"
        if mode in {"stop", "shutdown"}:
            assert runtime.recover().confirmed_live == 1
            if mode == "stop":
                assert runtime.request_stop(ref.profile_id, ref.run_id, 5)
            else:
                runtime.shutdown(5)
        process.communicate(timeout=8)
        assert ledger.get(ref).status is RunStatus.CLOSED
        with database.read() as db:
            receipt = db.execute(
                "SELECT target_revision FROM profile_sync_commits WHERE profile_id=? AND run_id=?",
                (ref.profile_id, ref.run_id),
            ).fetchone()
            outcome = db.execute(
                "SELECT storage_outcome FROM browser_run_placements WHERE run_id=?", (ref.run_id,)
            ).fetchone()
        if mode in {"crash", "invalid_identity"}:
            assert receipt is None
            assert ledger.lifecycle_snapshot() == {ref.profile_id: ("unsaved", "local")}
            if mode == "invalid_identity":
                assert (tmp_path / "data/alpha/fingerprint.json").read_bytes() == b"broken"
        else:
            assert receipt and receipt[0] == 1
            assert outcome and outcome[0] == "saved"
            assert ledger.lifecycle_snapshot() == {}
    finally:
        stop_owned(process)
        runtime.shutdown(0)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux supervisor protocol")
def test_local_slot_is_held_while_child_waits_to_journal(tmp_path):
    code = _BROWSER_PROTOCOL + "print('BROWSER_CLOSED',flush=True)"
    gate = tmp_path / "hash.lock"
    with catalog_lock(gate):
        database, ledger, ref, process, _ = start_supervisor(
            tmp_path, code, local_store=True, hash_gate=gate
        )
        try:
            assert line(process).startswith("CONTROL:")
            assert line(process) == "BROWSER_STARTED"
            assert line(process) == "BROWSER_CLOSED"
            assert line(process) == "reading-profile"
            with catalog_lock(tmp_path / "catalog.lock", timeout=0.2):
                assert (
                    ledger.reserve([ref.profile_id], ledger.get(ref).host_id).newly_reserved == ()
                )
            with database.read() as db:
                assert db.execute("SELECT COUNT(*) FROM profile_sync_commits").fetchone()[0] == 0
        except BaseException:
            process.kill()
            process.wait(timeout=5)
            raise
    process.communicate(timeout=5)
    assert process.returncode == 0 and ledger.get(ref).status is RunStatus.CLOSED
    with database.read() as db:
        assert db.execute("SELECT run_id FROM profile_sync_commits").fetchone()[0] == ref.run_id


def test_local_and_remote_capture_agree_on_changed_and_unchanged_revisions(tmp_path):
    cluster = _remote_fixture(tmp_path)
    old = cluster.store.capture(cluster.profile, cluster.ledger.get(cluster.run.ref))
    assert cluster.ledger.mark_remote_uncertain(cluster.run.ref, "worker-a", "boot-a", 1)
    assert cluster.ledger.complete_remote(
        cluster.run.ref, "worker-a", "boot-a", 1, process_stopped=True, quarantined=True
    )
    receipts = []
    for i, data in enumerate((None, b"second", b"third", None)):
        run = cluster.ledger.reserve([cluster.profile.profile_id], "master").runs[0]
        process = ProcessIdentity(921 + i, 401 + i)
        assert cluster.ledger.claim(run.ref, "master", process)
        assert cluster.ledger.mark_running(run.ref, track_local_data=True)
        if data:
            (cluster.profile_dir / "cookies.sqlite").write_bytes(data)
        assert cluster.ledger.release_after_exit(run.ref, process, lambda *_: ProcessLiveness.DEAD)
        receipts.append(cluster.store.record_local_stop(cluster.profile, run))
    assert [r.revision for r in receipts] == [
        old.base_revision,
        old.base_revision + 1,
        old.base_revision + 2,
        old.base_revision + 2,
    ]
    assert len({r.receipt_id for r in receipts}) == 4
    transfer = _remote_run(
        cluster.database,
        run_id="next-copy",
        profile_id=cluster.profile.profile_id,
        status="transferring",
    )
    with cluster.database.transaction() as db:
        db.execute(
            "UPDATE browser_run_placements SET base_revision=NULL WHERE run_id=?",
            (transfer.run_id,),
        )
    next_copy = cluster.store.capture(cluster.profile, cluster.ledger.get(transfer))
    assert next_copy.base_revision == receipts[-1].revision
    assert next_copy.archive_digest == cluster.store.revision(cluster.profile.profile_id)[1]


def test_inspected_subdirectory_identity_is_required_for_read(tmp_path):
    from foxprofile_ext.workers.profile_store import ProfileStoreError, _ProfileTree

    tree_root = tmp_path / "profile"
    directory = tree_root / "storage"
    directory.mkdir(parents=True)
    (directory / "state").write_bytes(b"first")
    tree = _ProfileTree(tree_root)
    ((parts, info),) = tree.files("camoufox")
    directory.rename(tree_root / "saved")
    directory.mkdir()
    (directory / "state").write_bytes(b"second")
    with pytest.raises(ProfileStoreError, match="directory changed"), tree.open_file(parts, info):
        pytest.fail("a replacement directory was accepted")


def test_promotion_needs_only_extraction_space_and_durable_cleanup(tmp_path, monkeypatch):
    from foxprofile_ext.workers.profile_store import pack_browser_data
    from tests.test_ext_worker_profile_store import _advance_to_syncing

    cluster = _remote_fixture(tmp_path)
    snapshot = cluster.store.capture(cluster.profile, cluster.ledger.get(cluster.run.ref))
    updated = tmp_path / "copy"
    shutil.copytree(cluster.profile_dir, updated)
    (updated / "cookies.sqlite").write_bytes(b"changed")
    upload = tmp_path / "return.zip"
    packed = pack_browser_data(updated, "camoufox", upload)
    run = _advance_to_syncing(cluster)
    checks = []

    def capacity(path):
        checks.append(Path(path))
        return SimpleNamespace(
            free=packed.expanded_bytes if Path(path) == cluster.store._journal_dir else 0
        )

    monkeypatch.setattr("foxprofile_ext.workers.profile_store.shutil.disk_usage", capacity)
    receipt = cluster.store.commit(
        cluster.profile, run, upload, snapshot.base_revision, packed.archive_digest
    )
    assert receipt.revision == 2 and receipt.archive_digest == packed.archive_digest
    assert checks == [cluster.store._journal_dir]
    assert (cluster.profile_dir / "cookies.sqlite").read_bytes() == b"changed"
    assert (
        cluster.store._existing_receipt(run.ref.profile_id, run.ref.run_id)["backup_name"] is None
    )
    assert cluster.store.recover() == ()


def test_rejected_stage_disposal_releases_sqlite_writer(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from foxprofile_ext.workers import profile_store
    from foxprofile_ext.workers.metrics import NodeMetrics
    from tests.test_ext_worker_profile_store import _advance_to_syncing

    cluster = _remote_fixture(tmp_path)
    snapshot = cluster.store.capture(cluster.profile, cluster.ledger.get(cluster.run.ref))
    run = _advance_to_syncing(cluster)
    extracted, disconnected, disposing, allow_removal = (Event() for _ in range(4))
    unpack = profile_store.unpack_browser_data
    remove = profile_store.shutil.rmtree

    def pause_after_unpack(*args, **kwargs):
        result = unpack(*args, **kwargs)
        extracted.set()
        assert disconnected.wait(5)
        return result

    def pause_during_removal(path, *args, **kwargs):
        if Path(path).parent == cluster.store._journal_dir and Path(path).name.startswith(
            "incoming-"
        ):
            disposing.set()
            assert allow_removal.wait(5)
        return remove(path, *args, **kwargs)

    monkeypatch.setattr(profile_store, "unpack_browser_data", pause_after_unpack)
    monkeypatch.setattr(profile_store.shutil, "rmtree", pause_during_removal)
    with ThreadPoolExecutor(max_workers=2) as pool:
        commit = pool.submit(
            cluster.store.commit,
            cluster.profile,
            run,
            snapshot.path,
            snapshot.base_revision,
            snapshot.archive_digest,
        )
        try:
            assert extracted.wait(5)
            assert cluster.ledger.mark_remote_uncertain(
                run.ref, run.worker_id, run.worker_boot_id, run.connection_generation
            )
            disconnected.set()
            assert disposing.wait(5)
            heartbeat = pool.submit(
                cluster.workers.heartbeat,
                run.worker_id,
                run.worker_boot_id,
                run.connection_generation,
                NodeMetrics(4096, None, 8192, ("camoufox",)),
            )
            heartbeat.result(timeout=1)
        finally:
            disconnected.set()
            allow_removal.set()
        with pytest.raises(profile_store.ProfileStoreError, match="no longer syncing"):
            commit.result(timeout=2)
    assert not tuple(cluster.store._journal_dir.glob("incoming-*"))
    assert (cluster.profile_dir / "cookies.sqlite").read_bytes() == b"cookie-v1"
