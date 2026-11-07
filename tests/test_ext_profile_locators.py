"""Canonical profile locators remain bound to one catalog identity."""

import pytest

from foxprofile_ext.errors import DomainError, ErrorCode
from foxprofile_ext.locking import catalog_lock
from tests.test_ext_access import access as access
from tests.test_ext_api_access import _login, _mutation_headers
from tests.test_ext_api_access import real_api as real_api


def test_canonical_locator_follows_rename_and_deduplicates_legacy_aliases(access):
    app = access["app"]
    actor = access["user_a"]
    original = app.resolve_profile(actor, "Profile A")
    locator = f"id:{original.profile_id}"
    app.update_profile(actor, locator, {"name": "Renamed"})
    replacement = app.create_profile(actor, name="Profile A")

    resolved = app.resolve_profile(actor, locator)
    assert (resolved.profile_id, resolved.name) == (original.profile_id, "Renamed")
    assert app.resolve_profile(actor, "Profile A").profile_id == replacement.profile_id
    assert app.resolve_profile(actor, original.profile_id) == resolved
    results = app.start_profiles(actor, (locator, original.profile_id, "Renamed"))
    assert [result.profile_id for result in results] == [original.profile_id]
    assert access["coordinator"].calls[0][1] == (resolved,)


def test_batch_profile_resolution_reloads_catalog_once(access, monkeypatch):
    app = access["app"]
    actor = access["user_a"]
    profiles = (
        app.resolve_profile(actor, "Profile A"),
        app.create_profile(actor, name="Second profile"),
        app.create_profile(actor, name="Third profile"),
    )
    manager = app._catalog.manager
    reload_profiles = manager.reload_profiles
    reload_count = 0

    def count_reload():
        nonlocal reload_count
        reload_count += 1
        reload_profiles()

    monkeypatch.setattr(manager, "reload_profiles", count_reload)
    identifiers = (profiles[0].profile_id, profiles[1].name, profiles[2].profile_id)
    with catalog_lock(app._catalog.lock_path):
        resolved = app._resolve_profiles_locked(actor, identifiers)

    assert reload_count == 1
    assert tuple(ref.profile_id for ref in resolved) == tuple(
        profile.profile_id for profile in profiles
    )


@pytest.mark.parametrize("operation", ["perform", "start", "stop", "control", "delete"])
def test_deleted_canonical_locator_never_falls_back_to_reused_uuid_name(access, operation):
    app = access["app"]
    actor = access["user_a"]
    original = app.resolve_profile(actor, "Profile A")
    app.delete_profile(actor, original.profile_id)
    replacement = app.create_profile(actor, name=original.profile_id)
    locator = f"id:{original.profile_id}"
    effects = []
    calls = {
        "perform": lambda: app.perform_profile(actor, locator, effects.append),
        "start": lambda: app.start_profile(actor, locator),
        "stop": lambda: app.stop_run(actor, locator),
        "control": lambda: app.control_run(actor, locator, "back"),
        "delete": lambda: app.delete_profile(actor, locator),
    }

    with pytest.raises(DomainError) as denied:
        calls[operation]()
    assert denied.value.code is ErrorCode.FORBIDDEN_OR_NOT_FOUND
    assert effects == []
    assert access["coordinator"].calls == []
    assert access["coordinator"].effect_calls == []
    # Raw locators retain the existing name/ID compatibility contract.
    assert app.resolve_profile(actor, original.profile_id).profile_id == replacement.profile_id


def test_foreign_canonical_locator_denied_before_any_batch_or_profile_effect(access):
    app = access["app"]
    actor = access["user_a"]
    own = app.resolve_profile(actor, "Profile A")
    foreign = app.resolve_profile(access["user_b"], "Profile B")
    effects = []
    locator = f"id:{foreign.profile_id}"
    calls = (
        lambda: app.perform_profile(actor, locator, effects.append),
        lambda: app.start_profiles(actor, (f"id:{own.profile_id}", locator)),
        lambda: app.stop_runs(actor, (f"id:{own.profile_id}", locator)),
        lambda: app.delete_profiles(actor, (f"id:{own.profile_id}", locator)),
    )
    for call in calls:
        with pytest.raises(DomainError) as denied:
            call()
        assert denied.value.code is ErrorCode.FORBIDDEN_OR_NOT_FOUND
    assert effects == []
    assert access["coordinator"].calls == []
    assert access["coordinator"].effect_calls == []
    assert app.resolve_profile(actor, own.profile_id).profile_id == own.profile_id


def test_encoded_canonical_locator_dispatches_profile_and_page_after_name_reuse(real_api):
    client, container, _, own, _ = real_api
    headers = _mutation_headers(_login(client, "alice")["csrf_token"])
    application = container.protected_application
    actor = container.auth_service.login("alice", "correct horse battery staple").principal
    locator = f"id%3A{own.profile_id}"
    renamed = client.patch(f"/api/v1/profiles/{locator}", json={"name": "Renamed"}, headers=headers)
    assert renamed.status_code == 200
    replacement = application.create_profile(actor, name="Own")
    detail = client.get(f"/api/v1/profiles/{locator}")
    assert detail.status_code == 200
    assert (detail.json()["profile_id"], detail.json()["name"]) == (own.profile_id, "Renamed")
    controls = []

    def control(principal, ref, *, action, params):
        controls.append((ref.profile_id, ref.name, action))
        return {"ok": True}

    container._Container__run_coordinator.control = control
    response = client.post(f"/api/v1/browser/{locator}/page/back", headers=headers)
    assert response.status_code == 200
    assert controls == [(own.profile_id, "Renamed", "back")]
    assert application.resolve_profile(actor, "Own").profile_id == replacement.profile_id


@pytest.mark.parametrize("operation", ["start", "stop", "delete"])
def test_canonical_batch_ids_reject_deleted_id_reused_as_name_before_effects(real_api, operation):
    client, container, _, own, _ = real_api
    headers = _mutation_headers(_login(client, "alice")["csrf_token"])
    app = container.protected_application
    actor = container.auth_service.login("alice", "correct horse battery staple").principal
    app.delete_profile(actor, own.profile_id)
    replacement = app.create_profile(actor, name=own.profile_id)
    other = app.create_profile(actor, name="Other")
    calls = []
    coordinator = container._Container__run_coordinator
    coordinator.reserve_and_start = lambda *args, **kwargs: calls.append(args)
    coordinator.stop = lambda *args, **kwargs: calls.append(args)
    path = "profiles/delete" if operation == "delete" else f"browser/{operation}"
    response = client.post(
        f"/api/v1/ext/{path}-batch",
        json={"profile_ids": [other.profile_id, own.profile_id]},
        headers=headers,
    )
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "FORBIDDEN_OR_NOT_FOUND"
    assert calls == []
    assert app.resolve_profile(actor, replacement.profile_id).profile_id == replacement.profile_id
    assert app.resolve_profile(actor, other.profile_id).profile_id == other.profile_id
