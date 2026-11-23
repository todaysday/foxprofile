"""Canonical metadata edits preserve local behavior and remote ownership."""

import sys

import pytest

from foxprofile_ext.dto import ProcessIdentity
from foxprofile_ext.errors import DomainError, ErrorCode
from tests.test_ext_access import access as access
from tests.test_ext_distributed_capacity import _remote_run


@pytest.mark.parametrize("remote", [False, True])
def test_os_metadata_edit_keeps_running_local_behavior_and_guards_remote_copy(access, remote):
    app = access["app"]
    profile = app.list_profiles(access["user_a"])[0]
    selected_os = "linux" if profile.os_type == "windows" else "windows"
    ledger = access["ledger"]
    if remote:
        ref = _remote_run(ledger.database, profile_id=profile.profile_id, status="running")
    else:
        ref = ledger.reserve((profile.profile_id,), "master").newly_reserved[0]
        assert ledger.claim(ref, "master", ProcessIdentity(12345, 1))
        assert ledger.mark_running(ref)
    before = ledger.get(ref)
    if remote:
        with pytest.raises(DomainError) as blocked:
            app.update_profile(access["user_a"], profile.profile_id, {"os_type": selected_os})
        assert blocked.value.code is ErrorCode.PROFILE_BUSY
    else:
        updated = app.update_profile(access["user_a"], profile.profile_id, {"os_type": selected_os})
        assert updated.os_type == selected_os
    assert ledger.get(ref) == before
    with pytest.raises(DomainError) as blocked:
        app.update_profile(access["user_a"], profile.profile_id, {"name": "Renamed"})
    assert blocked.value.code is ErrorCode.PROFILE_BUSY


@pytest.mark.skipif(sys.platform != "linux", reason="Linux lifecycle manager flock")
@pytest.mark.parametrize("failure", ["environment", "manifest"])
def test_failed_distributed_composition_releases_unpublished_manager(
    access, monkeypatch, tmp_path, failure
):
    from foxprofile_ext import runtime_factory
    from foxprofile_ext.workers.manifest import ManifestError

    monkeypatch.setattr(runtime_factory, "_installed_executable", lambda _: None)
    monkeypatch.setenv("FOXPROFILE_WORKERS_ENABLED", "true")
    monkeypatch.setenv(
        "FOXPROFILE_MASTER_URL",
        "http://master.example.test" if failure == "environment" else "https://master.example.test",
    )
    monkeypatch.setenv("FOXPROFILE_RUNTIME_MANIFEST_SHA256", "a" * 64)
    monkeypatch.setenv("FOXPROFILE_RUNTIME_MANIFEST", str(tmp_path / "missing-manifest.json"))
    monkeypatch.setenv("FOXPROFILE_NATIVE_EVIDENCE", str(tmp_path / "missing-evidence.json"))

    def build():
        return runtime_factory.build_run_coordinator(
            access["ledger"].database,
            access["catalog"],
            access["auth"],
            access["assignments"],
            access["ledger"],
        )

    retained = None
    try:
        with pytest.raises((ValueError, ManifestError)) as error:
            build()
        retained = error.value
        monkeypatch.setenv("FOXPROFILE_WORKERS_ENABLED", "false")
        coordinator = build()
        coordinator.shutdown(timeout=0)
    finally:
        # Also leave the pre-fix red run free of task-owned lock handles.
        traceback = retained.__traceback__ if retained is not None else None
        while traceback is not None:
            if traceback.tb_frame.f_code is runtime_factory.build_run_coordinator.__code__:
                runtime = traceback.tb_frame.f_locals.get("runtime")
                if runtime is not None:
                    runtime.shutdown(timeout=0)
            traceback = traceback.tb_next
