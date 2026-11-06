"""Actual Flet callbacks over disposable real authentication/catalog services."""

from types import SimpleNamespace

import flet as ft
import pytest

from foxprofile_ext.errors import DomainError, ErrorCode
from foxprofile_ext.request_context import current_principal
from src.core.container import _PrincipalScopedAdapter
from src.core.strings import STRINGS, get_string, set_language
from src.ui.app import App
from src.ui.dialogs.profile import open_profile_dialog
from tests.test_ext_access import access as access


class FakePage:
    def __init__(self):
        self.controls = []
        self.services = []
        self.overlay = []
        self.dialogs = []
        self.window = SimpleNamespace()
        self.tasks = []

    def add(self, control):
        self.controls.append(control)

    def update(self):
        pass

    def run_task(self, task):
        self.tasks.append(task)

    def show_dialog(self, dialog):
        dialog.open = True
        self.dialogs.append(dialog)

    def pop_dialog(self):
        for dialog in reversed(self.dialogs):
            if dialog.open:
                dialog.open = False
                return dialog
        return None


def controls(root):
    yield root
    for name in ("content", "controls", "title", "actions"):
        value = getattr(root, name, None)
        if isinstance(value, ft.Control):
            yield from controls(value)
        elif isinstance(value, list):
            for child in value:
                if isinstance(child, ft.Control):
                    yield from controls(child)


def button(root, text):
    return next(
        c
        for c in controls(root)
        if isinstance(c, (ft.Button, ft.OutlinedButton, ft.TextButton)) and c.content == text
    )


def field(root, label):
    return next(c for c in controls(root) if isinstance(c, ft.TextField) and c.label == label)


@pytest.fixture
def desktop(access, monkeypatch):
    monkeypatch.setenv("FOXPROFILE_EXT_ENABLED", "true")
    app = access["app"]
    manager = access["catalog"].manager

    class Container:
        auth_service = access["auth"]
        protected_application = app
        event_bus = SimpleNamespace(subscribe=lambda callback: None)

        @property
        def profile_manager(self):
            pytest.fail("Desktop accessed raw profile manager")

        @property
        def browser_launcher(self):
            pytest.fail("Desktop accessed raw launcher")

        @property
        def proxy_service(self):
            pytest.fail("Desktop accessed raw proxy service")

        def profile_manager_adapter(self):
            return _PrincipalScopedAdapter(manager, app, "profile_manager")

        def browser_launcher_adapter(self):
            return _PrincipalScopedAdapter(SimpleNamespace(), app, "browser_launcher")

        def proxy_service_adapter(self):
            return _PrincipalScopedAdapter(
                SimpleNamespace(check_proxy_sync=lambda *_: (True, "ok")), app, "proxy_service"
            )

    set_language("en")
    instance = App(Container())
    page = FakePage()
    instance._main(page)
    yield instance, page, access
    instance.personal.clear()
    set_language("vi")


def login(instance, page, username="alice", password="alice password"):
    root = page.controls[0]
    field(root, get_string("personal_username")).value = username
    field(root, get_string("personal_password")).value = password
    button(root, get_string("personal_sign_in")).on_click(None)
    assert instance.personal.session.binding is not None


@pytest.mark.parametrize("language", ["en", "vi"])
def test_entry_never_touches_raw_services_and_filters_rows(desktop, language):
    instance, page, _ = desktop
    assert instance.personal.session.binding is None
    instance._set_language(language)
    login(instance, page)
    assert list(instance.pm.profiles) == ["Profile A"]
    values = [c.value for c in controls(page.controls[0]) if isinstance(c, ft.Text)]
    assert "Profile A" in values
    assert "Profile B" not in values
    assert not any("secret" in str(v) for v in values)
    assert instance.refs.stats_text.value == get_string("total_profiles", count=1)
    with pytest.raises(DomainError, match="Authentication"):
        current_principal()


def test_logout_relogin_clears_private_state_and_rejects_old_confirm(desktop):
    instance, page, access = desktop
    login(instance, page)
    original_binding = instance.personal.session.binding
    old_handler = instance.h
    profile = instance.pm.list_profiles()[0]
    old_handler.on_delete(profile.profile_id)
    old_confirm = page.dialogs[-1].actions[-1].on_click
    instance.state.add_log("private A log")
    instance.state.select_all([profile.profile_id])
    instance.personal.clear()
    assert original_binding.session._token is None
    assert all(not d.open and d.content is None for d in page.dialogs)
    login(instance, page, "bob", "bob password")
    old_confirm(None)
    old_handler.log("late A log")
    assert access["catalog"].get_profile(profile.profile_id).name == "Profile A"
    assert instance.state.get_all_log_lines() == []
    assert instance.state.selected_names() == set()
    assert list(instance.pm.profiles) == ["Profile B"]


def test_revocation_removes_rows_selection_dialogs_and_logs(desktop):
    instance, page, access = desktop
    login(instance, page)
    instance.personal.reconcile()
    profile = instance.pm.list_profiles()[0]
    instance.h.on_edit(profile.profile_id)
    instance.state.add_log("private A log")
    instance.state.select_all([profile.profile_id])
    access["app"].replace_assignments(access["admin"], "user-a", [])
    assert instance.personal.reconcile()
    assert instance.pm.list_profiles() == []
    assert instance.state.get_all_log_lines() == []
    assert instance.state.selected_names() == set()
    assert not any(d.open for d in page.dialogs)


def test_disabled_session_returns_to_entry(desktop):
    instance, page, access = desktop
    login(instance, page)
    access["auth"].update_user_state(access["admin"], "user-a", active=False)
    assert not instance.personal.reconcile()
    assert instance.personal.session.binding is None
    assert instance.refs is None
    assert instance.pm is None
    assert field(page.controls[0], get_string("personal_password")).value == ""


def test_wrong_role_mcp_never_builds_credentials(desktop, monkeypatch):
    from src.services import mcp_setup

    instance, page, _ = desktop
    login(instance, page)
    monkeypatch.setattr(
        mcp_setup,
        "build_configs",
        lambda *_args, **_kwargs: pytest.fail("Credential config touched"),
    )
    instance._open_mcp()
    assert not page.dialogs
    assert "Administrator" in instance.state.get_all_log_lines()[-1]


def test_mixed_authority_bulk_start_has_no_effects(desktop):
    instance, page, access = desktop
    login(instance, page)
    mine = instance.pm.list_profiles()[0]
    foreign = next(p for p in access["catalog"].list_profiles() if p.name == "Profile B")
    instance.state.select_all([mine.profile_id, foreign.profile_id])
    instance.h.on_bulk_launch()
    assert access["coordinator"].calls == []
    instance.state.clear_selection()
    instance.state.select_all([mine.profile_id])
    instance.h.on_bulk_launch()
    assert len(access["coordinator"].calls) == 1
    assert access["coordinator"].calls[0][1][0].profile_id == mine.profile_id


def test_old_profile_id_cannot_read_replacement_fingerprint(desktop, monkeypatch):
    from src.services.browser import fingerprint

    instance, page, access = desktop
    login(instance, page)
    binding = instance.personal.session.binding
    profile = binding.pm.list_profiles()[0]
    binding.pm.delete_profile(profile.profile_id)
    access["app"].create_profile(access["user_b"], name=profile.name)
    reads = []
    monkeypatch.setattr(fingerprint, "summary", lambda path: reads.append(path))
    with pytest.raises(DomainError) as error:
        binding.fingerprint(profile.profile_id)
    assert error.value.code is ErrorCode.FORBIDDEN_OR_NOT_FOUND
    assert reads == []


def test_deleted_desktop_id_cannot_target_replacement_named_after_uuid(desktop, tmp_path):
    instance, page, access = desktop
    login(instance, page)
    binding = instance.personal.session.binding
    profile = binding.pm.list_profiles()[0]
    identifier = profile.profile_id
    authorize = instance.personal.private_authorizer(binding, identifier)
    binding.pm.delete_profile(identifier)
    replacement = access["app"].create_profile(access["user_a"], name=identifier)
    effects = (
        authorize,
        lambda: binding.profile(identifier),
        lambda: binding.pm.update_profile(identifier, "Changed", "", "linux"),
        lambda: binding.pm.delete_profile(identifier),
        lambda: binding.pm.export_profile(identifier, str(tmp_path)),
        lambda: binding.fingerprint(identifier),
        lambda: binding.fingerprint(identifier, reset=True),
        lambda: binding.cookies(identifier),
        lambda: binding.bl.is_running(identifier),
        lambda: binding.bl.start_profiles([identifier]),
        lambda: binding.bl.stop_profile(identifier),
        lambda: binding.bl.stop_profiles([identifier]),
    )
    for effect in effects:
        with pytest.raises(DomainError) as denied:
            effect()
        assert denied.value.code is ErrorCode.FORBIDDEN_OR_NOT_FOUND
    assert access["coordinator"].calls == []
    assert access["coordinator"].effect_calls == []
    assert access["catalog"].get_profile(replacement.profile_id).name == identifier


def test_deleted_desktop_bulk_selection_cannot_delete_uuid_name_replacement(desktop):
    instance, page, access = desktop
    login(instance, page)
    binding = instance.personal.session.binding
    identifier = binding.pm.list_profiles()[0].profile_id
    instance.state.select_all([identifier])
    instance.h.on_bulk_delete()
    confirm = page.dialogs[-1].actions[-1].on_click
    binding.pm.delete_profile(identifier)
    replacement = access["app"].create_profile(access["user_a"], name=identifier)

    confirm(None)

    assert access["catalog"].get_profile(replacement.profile_id).name == identifier
    assert access["coordinator"].calls == []
    assert access["coordinator"].effect_calls == []


def test_new_engine_os_defaults_preserve_explicit_selection():
    page = FakePage()
    open_profile_dialog(page, SimpleNamespace(), lambda *_: None, authorize=lambda: None)
    dropdowns = [c for c in controls(page.dialogs[-1]) if isinstance(c, ft.Dropdown)]
    os_choice = next(c for c in dropdowns if c.value == "linux")
    engine = next(c for c in dropdowns if c.value == "camoufox")
    engine.value = "chrome"
    engine.on_select(None)
    assert os_choice.value == "windows"
    os_choice.value = "macos"
    os_choice.on_select(None)
    engine.value = "camoufox"
    engine.on_select(None)
    assert os_choice.value == "macos"


def test_all_new_strings_have_both_languages():
    keys = {key for key in STRINGS["en"] if key.startswith("personal_")}
    assert keys == {key for key in STRINGS["vi"] if key.startswith("personal_")}
    assert all(STRINGS[language][key] for language in ("vi", "en") for key in keys)


def test_expired_session_clears_cookie_and_private_controls(desktop, monkeypatch):
    import time

    instance, page, access = desktop
    login(instance, page)
    instance.h.on_edit(instance.pm.list_profiles()[0].profile_id)
    monkeypatch.setattr(access["auth"], "_clock", lambda: time.time() + 13 * 3600)
    assert not instance.personal.reconcile()
    assert instance.personal.session._token is None
    assert not any(d.open for d in page.dialogs)
    assert instance.state.get_all_log_lines() == []


def test_starting_and_stopping_stamps_keep_stop_available(desktop):
    from foxprofile_ext.dto import ProcessIdentity

    instance, page, access = desktop
    login(instance, page)
    profile = instance.pm.list_profiles()[0]
    run = access["ledger"].reserve([profile.profile_id], "test-host").newly_reserved[0]
    access["ledger"].claim(run, "test-host", ProcessIdentity(12345, 1))
    instance._refresh_profiles()
    row = instance.refs.profile_list_area.controls[0]
    text = [c.value for c in controls(row) if isinstance(c, ft.Text)]
    assert get_string("stamp_starting").upper() in text
    assert get_string("stamp_live").upper() not in text
    assert not button(row, get_string("stop")).disabled
    access["ledger"].mark_stopping(run)
    instance._refresh_profiles()
    row = instance.refs.profile_list_area.controls[0]
    assert get_string("personal_state_stopping").upper() in [
        c.value for c in controls(row) if isinstance(c, ft.Text)
    ]
    assert not button(row, get_string("stop")).disabled


def test_legacy_desktop_import_does_not_require_optional_auth_dependency(tmp_path):
    import os
    import subprocess
    import sys

    script = """
import importlib.abc
import sys
class RejectAuth(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "argon2" or fullname.startswith("argon2."):
            raise ModuleNotFoundError("optional auth dependency unavailable")
sys.meta_path.insert(0, RejectAuth())
import src.ui.app
assert "argon2" not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        env={**os.environ, "FOXPROFILE_EXT_ENABLED": "false"},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_cookie_export_callback_cannot_follow_later_login(desktop, tmp_path, monkeypatch):
    import asyncio

    from src.services.browser import cookies

    instance, page, _ = desktop
    login(instance, page)
    profile = instance.pm.list_profiles()[0]
    output = tmp_path / "cookies.json"

    async def choose_file(**_):
        instance.personal.clear()
        login(instance, page, "bob", "bob password")
        return str(output)

    instance.refs.file_picker = SimpleNamespace(save_file=choose_file)
    monkeypatch.setattr(
        cookies, "export_cookies", lambda *_: pytest.fail("Old session reached cookie helper")
    )
    instance.h.on_cookies(profile.profile_id)
    callback = button(page.dialogs[-1], get_string("cookie_export_json")).on_click
    asyncio.run(callback(None))
    assert not output.exists()
    assert list(instance.pm.profiles) == ["Profile B"]
    assert instance.state.get_all_log_lines() == []


def test_new_profile_dialog_callback_creates_and_assigns(desktop):
    instance, page, access = desktop
    login(instance, page)
    instance.h.open_add_dialog()
    dialog = page.dialogs[-1]
    name = next(
        c
        for c in controls(dialog)
        if isinstance(c, ft.TextField) and c.hint_text == get_string("enter_profile_name")
    )
    name.value = "New profile"
    button(dialog, get_string("create_profile_btn")).on_click(None)
    created = next(p for p in instance.pm.list_profiles() if p.name == "New profile")
    assert created.os_type == "linux"
    assert access["assignments"].is_assigned("user-a", created.profile_id)
    assert not access["assignments"].is_assigned("user-b", created.profile_id)


def test_native_archive_io_uses_managed_streams_and_creator_grants(desktop, tmp_path):
    import json
    import zipfile
    from dataclasses import replace

    from foxprofile_ext.storage import TransferHandleDAO
    from foxprofile_ext.transfer import ManagedProfileTransfer

    instance, page, access = desktop
    app = access["app"]
    app._transfers = ManagedProfileTransfer(
        access["catalog"],
        TransferHandleDAO(access["ledger"].database),
        tmp_path / "staging",
        lambda actor, profile_id: app.perform_profile(actor, profile_id, replace),
    )
    login(instance, page)
    profile = instance.pm.list_profiles()[0]
    ok, path = instance.pm.export_profile(profile.profile_id, tmp_path, False)
    assert ok
    with zipfile.ZipFile(path) as archive:
        metadata = json.loads(archive.read("profile.json"))
    assert "profile_id" not in metadata
    metadata["name"] = "Imported profile"
    client_input = tmp_path / "client-input.zip"
    with zipfile.ZipFile(client_input, "w") as archive:
        archive.writestr("profile.json", json.dumps(metadata))
    assert instance.pm.import_profile(client_input) == (True, "Imported profile")
    imported = next(p for p in instance.pm.list_profiles() if p.name == "Imported profile")
    assert imported.profile_id != profile.profile_id
    assert access["assignments"].is_assigned("user-a", imported.profile_id)


def test_old_sidebar_callbacks_cannot_open_new_actor_dialogs(desktop):
    instance, page, _ = desktop
    login(instance, page)
    old_root = page.controls[0]
    old_new = button(old_root, get_string("new_profile")).on_click
    old_logout = button(old_root, get_string("personal_sign_out")).on_click
    instance.personal.clear()
    login(instance, page, "bob", "bob password")
    old_new(None)
    old_logout(None)
    assert (
        instance.personal.session.auth.current_user(
            instance.personal.session.binding.actor()
        ).username
        == "bob"
    )
    assert not any(dialog.open for dialog in page.dialogs)


def test_runtime_state_change_refreshes_without_name_change(desktop, monkeypatch):
    import asyncio

    import src.ui.app as app_module
    from foxprofile_ext.dto import ProcessIdentity

    instance, page, access = desktop
    login(instance, page)
    instance.personal.reconcile()
    profile = instance.pm.list_profiles()[0]
    run = access["ledger"].reserve([profile.profile_id], "test-host").newly_reserved[0]
    access["ledger"].claim(run, "test-host", ProcessIdentity(12345, 1))
    instance.state._last_running_snapshot = instance.personal.run_snapshot()
    instance._refresh_profiles()
    access["ledger"].mark_running(run)

    async def stop_loop(_):
        instance.page = None

    monkeypatch.setattr(app_module.asyncio, "sleep", stop_loop)
    asyncio.run(instance._ui_reconcile_loop())
    instance.page = page
    row = instance.refs.profile_list_area.controls[0]
    assert get_string("stamp_live").upper() in [
        c.value for c in controls(row) if isinstance(c, ft.Text)
    ]


def test_admin_controls_create_assign_reset_and_capacity(desktop):
    from foxprofile_ext.storage import SettingsDAO

    instance, page, access = desktop
    access["app"]._settings = SettingsDAO(access["ledger"].database)
    login(instance, page, "admin", "admin password")
    instance.personal.open_admin(instance.personal.session.binding)
    dialog = page.dialogs[-1]
    field(dialog, get_string("personal_username")).value = "charlie"
    field(dialog, get_string("personal_password")).value = "charlie password"
    button(dialog, get_string("personal_create_user")).on_click(None)
    new_user = next(
        user
        for user in access["auth"].list_users(instance.personal.session.binding.actor())
        if user.username == "charlie"
    )
    dialog = page.dialogs[-1]
    row = next(
        c
        for c in controls(dialog)
        if isinstance(c, ft.Column)
        and any(isinstance(child, ft.Text) and child.value == "charlie" for child in c.controls)
    )
    checkbox = next(
        c for c in controls(row) if isinstance(c, ft.Checkbox) and c.label == "Profile A"
    )
    checkbox.value = True
    button(row, get_string("personal_save_assignments")).on_click(None)
    assert access["assignments"].profiles_for_user(new_user.user_id) == (checkbox.data,)
    dialog = page.dialogs[-1]
    row = next(
        c
        for c in controls(dialog)
        if isinstance(c, ft.Column)
        and any(isinstance(child, ft.Text) and child.value == "charlie" for child in c.controls)
    )
    field(row, get_string("personal_reset_password")).value = "replacement password"
    button(row, get_string("personal_reset_password")).on_click(None)
    issued = access["auth"].login("charlie", "replacement password")
    assert issued.user.user_id == new_user.user_id
    access["auth"].logout(issued.principal)
    access["ledger"].reserve([p.profile_id for p in access["catalog"].list_profiles()], "test-host")
    dialog = page.dialogs[-1]
    capacity = field(dialog, get_string("personal_capacity"))
    capacity.value = "1"
    capacity_row = next(
        c for c in controls(dialog) if isinstance(c, ft.Row) and capacity in c.controls
    )
    button(capacity_row, get_string("save_changes")).on_click(None)
    assert (
        access["app"]
        .admin_settings(instance.personal.session.binding.actor())
        .max_concurrent_profiles
        == 1
    )
    assert len(access["ledger"].active_runs()) == 3


def test_admin_controls_reject_last_admin_and_invalid_capacity(desktop):
    from foxprofile_ext.storage import SettingsDAO

    instance, page, access = desktop
    access["app"]._settings = SettingsDAO(access["ledger"].database)
    login(instance, page, "admin", "admin password")
    instance.personal.open_admin(instance.personal.session.binding)
    dialog = page.dialogs[-1]
    admin_row = next(
        c
        for c in controls(dialog)
        if isinstance(c, ft.Column)
        and any(isinstance(child, ft.Text) and child.value == "admin" for child in c.controls)
    )
    role = next(c for c in controls(admin_row) if isinstance(c, ft.Dropdown))
    role.value = "user"
    button(admin_row, get_string("save_changes")).on_click(None)
    assert (
        access["auth"].current_user(instance.personal.session.binding.actor()).role.value == "admin"
    )
    assert get_string("personal_error_validation_error") in [
        c.value for c in controls(dialog) if isinstance(c, ft.Text)
    ]
    capacity = field(dialog, get_string("personal_capacity"))
    capacity.value = "0"
    capacity_row = next(
        c for c in controls(dialog) if isinstance(c, ft.Row) and capacity in c.controls
    )
    button(capacity_row, get_string("save_changes")).on_click(None)
    assert (
        access["app"]
        .admin_settings(instance.personal.session.binding.actor())
        .max_concurrent_profiles
        == 10
    )


def test_mixed_authority_bulk_stop_delete_have_no_effects(desktop):
    instance, page, access = desktop
    login(instance, page)
    profiles = access["catalog"].list_profiles()
    mine = next(p for p in profiles if p.name == "Profile A")
    foreign = next(p for p in profiles if p.name == "Profile B")
    instance.state.select_all([mine.profile_id, foreign.profile_id])
    instance.h.on_bulk_stop()
    assert access["coordinator"].effect_calls == []
    instance.h.on_bulk_delete()
    page.dialogs[-1].actions[-1].on_click(None)
    assert {p.profile_id for p in access["catalog"].list_profiles()} == {
        p.profile_id for p in profiles
    }


def test_capacity_error_shows_limit_without_foreign_details(desktop, monkeypatch):
    from foxprofile_ext.errors import ProfileCapacityExceeded

    instance, page, access = desktop
    login(instance, page)
    profile = instance.pm.list_profiles()[0]

    def full(*_args, **_kwargs):
        raise ProfileCapacityExceeded(3)

    monkeypatch.setattr(access["coordinator"], "reserve_and_start", full)
    instance.state.select_all([profile.profile_id])
    instance.h.on_bulk_launch()
    line = instance.state.get_all_log_lines()[-1]
    assert "(3)" in line
    assert "Profile B" not in line
    assert "retry" in line


def test_summary_is_masked_but_authorized_edit_retains_credentials(desktop):
    instance, page, access = desktop
    login(instance, page)
    profile = instance.pm.list_profiles()[0]
    assert profile.proxy == "http://192.0.2.1:8080"
    instance.h.on_edit(profile.profile_id)
    dialog = page.dialogs[-1]
    proxy = next(
        c
        for c in controls(dialog)
        if isinstance(c, ft.TextField) and c.value == "http://a:secret@192.0.2.1:8080"
    )
    proxy.value = "http://user:pass@192.0.2.4:8080"
    button(dialog, get_string("save_changes")).on_click(None)
    assert (
        access["catalog"].get_profile(profile.profile_id).proxy == "http://user:pass@192.0.2.4:8080"
    )
    assert proxy.value == ""
    assert instance.pm.list_profiles()[0].proxy == "http://192.0.2.4:8080"
    with pytest.raises(DomainError):
        current_principal()


def test_admin_assignments_load_current_selection_before_replacement(desktop, monkeypatch):
    from foxprofile_ext.storage import SettingsDAO

    instance, page, access = desktop
    app = access["app"]
    app._settings = SettingsDAO(access["ledger"].database)
    reads, writes = [], []
    read = app.assignments_for_user
    write = app.replace_assignments

    def load(actor, user_id):
        reads.append(user_id)
        return read(actor, user_id)

    def replace(actor, user_id, profile_ids):
        assert user_id in reads
        writes.append((user_id, tuple(profile_ids)))
        return write(actor, user_id, profile_ids)

    monkeypatch.setattr(app, "assignments_for_user", load)
    monkeypatch.setattr(app, "replace_assignments", replace)
    login(instance, page, "admin", "admin password")
    instance.personal.open_admin(instance.personal.session.binding)
    dialog = page.dialogs[-1]
    alice = next(
        c
        for c in controls(dialog)
        if isinstance(c, ft.Column)
        and any(isinstance(child, ft.Text) and child.value == "alice" for child in c.controls)
    )
    selected = [
        c.data for c in controls(alice) if isinstance(c, ft.Checkbox) and c.data and c.value
    ]
    assert selected == list(access["assignments"].profiles_for_user("user-a"))
    button(alice, get_string("personal_save_assignments")).on_click(None)
    assert writes == [("user-a", tuple(selected))]


def test_live_admin_handler_rechecks_revoked_role(desktop):
    from foxprofile_ext.storage import SettingsDAO

    instance, page, access = desktop
    auth = access["auth"]
    access["app"]._settings = SettingsDAO(access["ledger"].database)
    auth.create_user(access["admin"], "another-admin", "another admin password", "admin")
    login(instance, page, "admin", "admin password")
    instance.personal.open_admin(instance.personal.session.binding)
    dialog = page.dialogs[-1]
    field(dialog, get_string("personal_username")).value = "denied-user"
    field(dialog, get_string("personal_password")).value = "denied user password"
    create = button(dialog, get_string("personal_create_user")).on_click
    auth.update_user_state(access["admin"], "admin-0", role="user")
    create(None)
    assert instance.personal.session.binding is None
    other = auth.login("another-admin", "another admin password")
    assert "denied-user" not in {user.username for user in auth.list_users(other.principal)}
    auth.logout(other.principal)


def test_new_private_dialog_scrubs_credentials_and_rejects_old_submit(desktop):
    instance, page, access = desktop
    login(instance, page)
    profile = instance.pm.list_profiles()[0]
    instance.h.on_edit(profile.profile_id)
    dialog = page.dialogs[-1]
    proxy = next(
        c for c in controls(dialog) if isinstance(c, ft.TextField) and "secret" in str(c.value)
    )
    save = button(dialog, get_string("save_changes")).on_click
    instance.h.open_add_dialog()
    assert not dialog.open
    assert dialog.content is None
    assert proxy.value == ""
    save(None)
    assert access["catalog"].get_profile(profile.profile_id).proxy.endswith("192.0.2.1:8080")
    assert page.dialogs[-1].open


def test_new_modal_rejects_old_delete_confirmation(desktop):
    instance, page, access = desktop
    login(instance, page)
    profile = instance.pm.list_profiles()[0]
    instance.h.on_delete(profile.profile_id)
    confirm = page.dialogs[-1].actions[-1].on_click
    instance.h.on_edit(profile.profile_id)
    confirm(None)
    assert access["catalog"].get_profile(profile.profile_id).name == profile.name
    assert page.dialogs[-1].open


def test_cookie_file_picker_response_cannot_follow_private_dialog_switch(
    desktop, tmp_path, monkeypatch
):
    import asyncio

    from src.services.browser import cookies

    instance, page, _ = desktop
    login(instance, page)
    profile = instance.pm.list_profiles()[0]
    output = tmp_path / "cookies.json"

    async def choose_file(**_):
        instance.h.open_add_dialog()
        return str(output)

    instance.refs.file_picker = SimpleNamespace(save_file=choose_file)
    monkeypatch.setattr(cookies, "export_cookies", lambda *_: pytest.fail("Stale export ran"))
    instance.h.on_cookies(profile.profile_id)
    callback = button(page.dialogs[-1], get_string("cookie_export_json")).on_click
    asyncio.run(callback(None))
    assert not output.exists()
    assert instance.state.get_all_log_lines() == []
    assert page.dialogs[-1].open


def test_ip_check_does_not_publish_after_private_dialog_switch(desktop, monkeypatch):
    import src.ui.dialogs.profile as dialogs

    instance, page, _ = desktop
    login(instance, page)
    profile = instance.pm.list_profiles()[0]
    instance.h.on_edit(profile.profile_id)
    dialog = page.dialogs[-1]
    old_result = next(
        c for c in controls(dialog) if isinstance(c, ft.Column) and c.scroll == ft.ScrollMode.AUTO
    )

    class ImmediateThread:
        def __init__(self, target, **_):
            self.target = target

        def start(self):
            self.target()

    def check(*_):
        instance.h.open_add_dialog()
        return SimpleNamespace(exit_ip="192.0.2.99")

    monkeypatch.setattr(dialogs.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(dialogs, "check_geo", check)
    button(dialog, get_string("check_ip")).on_click(None)
    assert old_result.controls == []
    assert not old_result.visible
    assert page.dialogs[-1].open


def test_personal_admin_keeps_mcp_guide_and_clears_csrf_on_logout(desktop, monkeypatch):
    from src.ui.dialogs import mcp_guide

    instance, page, _ = desktop
    login(instance, page, "admin", "admin password")
    session = instance.personal.session
    assert session._token and session._csrf_token
    monkeypatch.setattr(
        mcp_guide.threading, "Thread", lambda **_: SimpleNamespace(start=lambda: None)
    )
    instance._open_mcp()
    assert page.dialogs[-1].open
    assert button(page.dialogs[-1], get_string("mcp_copy"))
    instance.personal.clear()
    assert session._token is None
    assert session._csrf_token is None


@pytest.mark.parametrize("target", ["foreign", "replaced"])
def test_fingerprint_reset_denies_foreign_or_replaced_identity_before_file_effect(
    desktop, monkeypatch, target
):
    from src.services.browser import fingerprint

    instance, page, access = desktop
    login(instance, page)
    binding = instance.personal.session.binding
    own = instance.pm.list_profiles()[0]
    if target == "foreign":
        identifier = next(
            p.profile_id for p in access["catalog"].list_profiles() if p.name == "Profile B"
        )
    else:
        identifier = own.profile_id
        binding.pm.delete_profile(identifier)
        access["app"].create_profile(access["user_b"], name=own.name)
    monkeypatch.setattr(fingerprint, "reset", lambda *_: pytest.fail("Unauthorized file effect"))
    with pytest.raises(DomainError) as error:
        binding.fingerprint(identifier, reset=True)
    assert error.value.code is ErrorCode.FORBIDDEN_OR_NOT_FOUND
    with pytest.raises(DomainError):
        current_principal()


def test_fingerprint_reset_callback_stays_inside_authorized_scope(desktop, monkeypatch, tmp_path):
    from contextlib import contextmanager

    from src.core import config
    from src.services.browser import fingerprint

    instance, page, _ = desktop
    login(instance, page)
    binding = instance.personal.session.binding
    profile = instance.pm.list_profiles()[0]
    effects = []
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)

    @contextmanager
    def exclusive(name):
        assert name == profile.name
        assert current_principal().subject_id == "user-a"
        yield
        assert current_principal().subject_id == "user-a"

    def reset(path):
        assert current_principal().subject_id == "user-a"
        effects.append(path)
        return True

    binding.session._launcher_reader = SimpleNamespace(exclusive=exclusive)
    monkeypatch.setattr(fingerprint, "reset", reset)
    instance.h.on_cookies(profile.profile_id)
    button(page.dialogs[-1], get_string("fingerprint_reset")).on_click(None)
    confirm = page.dialogs[-1].actions[-1].on_click
    confirm(None)
    assert effects == [str(tmp_path / profile.name)]
    with pytest.raises(DomainError):
        current_principal()
    instance.h.on_cookies(profile.profile_id)
    button(page.dialogs[-1], get_string("fingerprint_reset")).on_click(None)
    stale_confirm = page.dialogs[-1].actions[-1].on_click
    instance.h.on_edit(profile.profile_id)
    stale_confirm(None)
    assert effects == [str(tmp_path / profile.name)]


def test_fingerprint_reset_busy_guard_precedes_file_effect(desktop, monkeypatch):
    from src.services.browser import fingerprint

    instance, page, access = desktop
    login(instance, page)
    profile = instance.pm.list_profiles()[0]
    access["ledger"].reserve([profile.profile_id], "test-host")
    monkeypatch.setattr(fingerprint, "reset", lambda *_: pytest.fail("Running profile reset"))
    with pytest.raises(DomainError) as error:
        instance.personal.session.binding.fingerprint(profile.profile_id, reset=True)
    assert error.value.code is ErrorCode.PROFILE_BUSY
    with pytest.raises(DomainError):
        current_principal()
