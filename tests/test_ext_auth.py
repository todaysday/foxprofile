"""Account and personal-session behavior independent of HTTP or browser code."""

import hashlib

import pytest

from foxprofile_ext.auth import AuthService, UserDTO, hash_password
from foxprofile_ext.bootstrap import initialize_admin
from foxprofile_ext.dto import PrincipalDTO, PrincipalKind, Role
from foxprofile_ext.errors import DomainError, ErrorCode
from foxprofile_ext.storage.dao import (
    AssignmentDAO,
    BootstrapAlreadyConfigured,
    LastAdministratorError,
    SessionDAO,
    StorageConflict,
    TransferHandleDAO,
    UserDAO,
)
from foxprofile_ext.storage.database import ExtensionDatabase

SERVICE = PrincipalDTO("trusted-agent", PrincipalKind.SERVICE)


def _service(tmp_path, now=None, valid_profiles=None):
    database = ExtensionDatabase(tmp_path / "extension.sqlite3")
    users = UserDAO(database)
    sessions = SessionDAO(database)
    assignments = AssignmentDAO(database)
    clock = now if now is not None else [1000.0]
    service = AuthService(
        users,
        sessions,
        assignments,
        lambda profile_id: profile_id in (valid_profiles or set()),
        clock=lambda: clock[0],
        session_ttl=60,
    )
    return service, users, sessions, assignments, clock


def _add_admin(users):
    return users.bootstrap_admin("admin-a", "admin", hash_password("admin password"))


def _login(service, username="admin", password="admin password"):
    return service.login(username, password)


def test_login_uses_argon2_hashes_and_exposes_only_public_user_fields(tmp_path):
    service, users, sessions, _, now = _service(tmp_path)
    account = _add_admin(users)

    issued = _login(service)
    record = sessions.get_active(
        hashlib.sha256(issued.session_token.encode("ascii")).hexdigest(), now[0]
    )

    assert account.password_hash.startswith("$argon2id$")
    assert account.password_hash != "admin password"
    assert isinstance(issued.user, UserDTO)
    assert issued.user.role is Role.ADMIN
    assert set(issued.user.__dataclass_fields__) == {"user_id", "username", "role", "active"}
    assert record is not None
    assert record.token_hash != issued.session_token
    assert issued.session_token not in repr(issued)
    assert issued.csrf_token not in repr(issued)
    assert account.password_hash not in repr(issued.user)


def test_missing_disabled_and_wrong_credentials_share_one_auth_error(tmp_path):
    service, users, _, _, _ = _service(tmp_path)
    _add_admin(users)
    users.create("user-a", "disabled", hash_password("disabled password"))
    users.set_active("user-a", False)
    attempts = (
        ("missing", "password"),
        ("admin", "wrong password"),
        ("disabled", "disabled password"),
        ("admin", ""),
    )

    errors = []
    for username, password in attempts:
        with pytest.raises(DomainError) as error:
            service.login(username, password)
        errors.append(error.value)

    assert {error.code for error in errors} == {ErrorCode.AUTH_REQUIRED}
    assert len({str(error) for error in errors}) == 1
    assert "missing" not in str(errors[0])
    assert "disabled" not in str(errors[0])


def test_sessions_expire_revoke_and_use_session_bound_csrf(tmp_path):
    service, users, sessions, _, now = _service(tmp_path)
    _add_admin(users)
    issued = _login(service)
    context = service.resolve_session(issued.session_token)

    assert context.principal.session_id == issued.principal.session_id
    assert context.csrf_token == issued.csrf_token
    assert service.csrf_token_for_session(issued.session_token) == issued.csrf_token
    assert service.verify_csrf(issued.session_token, issued.csrf_token)
    assert not service.verify_csrf(issued.session_token, "wrong-csrf-token")
    with pytest.raises(DomainError) as invalid_session:
        service.verify_csrf("z" * 43, issued.csrf_token)
    assert invalid_session.value.code is ErrorCode.AUTH_REQUIRED

    service.logout(context.principal)
    assert sessions.get_valid_by_id(context.principal.session_id, now[0]) is None
    with pytest.raises(DomainError) as revoked:
        service.resolve_session(issued.session_token)
    assert revoked.value.code is ErrorCode.AUTH_REQUIRED

    expiring = _login(service)
    now[0] += 60
    with pytest.raises(DomainError) as expired:
        service.resolve_session(expiring.session_token)
    assert expired.value.code is ErrorCode.AUTH_REQUIRED


def test_admin_service_revalidates_sessions_and_never_promotes_service_principal(tmp_path):
    service, users, _, _, _ = _service(tmp_path)
    _add_admin(users)
    users.create("user-a", "member", hash_password("old member password"))
    admin = _login(service)
    member = _login(service, "member", "old member password")

    for principal in (member.principal, SERVICE):
        with pytest.raises(DomainError) as denied:
            service.create_user(principal, "new-account", "new password")
        assert denied.value.code is ErrorCode.ADMIN_REQUIRED

    changed = service.reset_password(admin.principal, "user-a", "new member password")
    assert changed == UserDTO("user-a", "member", Role.USER, True)
    with pytest.raises(DomainError) as old_session:
        service.validate_principal(member.principal)
    assert old_session.value.code is ErrorCode.AUTH_REQUIRED
    assert service.login("member", "new member password").user == changed


def test_combined_role_and_active_update_is_atomic_and_revokes_old_admin_session(tmp_path):
    service, users, _, _, _ = _service(tmp_path)
    _add_admin(users)
    first_admin = _login(service)

    with pytest.raises(DomainError) as last_admin:
        service.update_user_state(first_admin.principal, "admin-a", role=Role.USER, active=False)
    assert last_admin.value.code is ErrorCode.VALIDATION_ERROR
    current = users.get("admin-a")
    assert current is not None and current.role is Role.ADMIN and current.active

    service.create_user(first_admin.principal, "admin-two", "admin-two password", Role.ADMIN)
    updated = service.update_user_state(
        first_admin.principal, "admin-a", role=Role.USER, active=False
    )
    assert updated == UserDTO("admin-a", "admin", Role.USER, False)
    with pytest.raises(DomainError) as revoked:
        service.list_users(first_admin.principal)
    assert revoked.value.code is ErrorCode.AUTH_REQUIRED

    second_admin = _login(service, "admin-two", "admin-two password")
    assert second_admin.user.role is Role.ADMIN
    assert [user.username for user in service.list_users(second_admin.principal)] == [
        "admin",
        "admin-two",
    ]


def test_user_dao_combines_account_changes_before_last_admin_check(tmp_path):
    database = ExtensionDatabase(tmp_path / "state.sqlite3")
    users = UserDAO(database)
    users.bootstrap_admin("admin-a", "admin-a", "hash-a")
    users.create("admin-b", "admin-b", "hash-b", Role.ADMIN)
    sessions = SessionDAO(database)
    session = sessions.create("session-a", "admin-a", "digest-a", 10, 100)

    changed = users.update_state("admin-a", role=Role.USER, active=False)

    assert changed.role is Role.USER and not changed.active
    assert sessions.get_valid_by_id(session.session_id, 50) is None
    assert users.get("admin-b").role is Role.ADMIN
    with pytest.raises(LastAdministratorError):
        users.update_state("admin-b", role=Role.USER, active=False)
    remaining = users.get("admin-b")
    assert remaining.role is Role.ADMIN and remaining.active


def test_session_creation_rejects_password_reset_after_credentials_were_read(tmp_path):
    database = ExtensionDatabase(tmp_path / "state.sqlite3")
    users = UserDAO(database)
    account = users.bootstrap_admin("admin-a", "admin-a", "old-hash")
    sessions = SessionDAO(database)
    users.set_password_hash("admin-a", "new-hash")

    with pytest.raises(StorageConflict):
        sessions.create(
            "session-a",
            account.user_id,
            "session-digest",
            10,
            100,
            expected_auth_version=account.auth_version,
        )


def test_expired_transfer_handles_are_enumerated_then_removed_with_expiry_guard(tmp_path):
    database = ExtensionDatabase(tmp_path / "state.sqlite3")
    users = UserDAO(database)
    users.bootstrap_admin("admin-a", "admin-a", "hash")
    users.create("user-a", "user-a", "hash")
    handles = TransferHandleDAO(database)
    active = handles.issue("handle-active", "user-a", "profile-a", "/private/a", 20)
    handles.issue("handle-revoked", "user-a", "profile-a", "/private/b", 30)
    handles.revoke("handle-revoked")

    assert handles.expired(19) == ()
    assert handles.remove_expired("handle-active", 19) is None
    expired = handles.expired(30)
    assert [handle.handle_id for handle in expired] == ["handle-active", "handle-revoked"]
    assert expired[1].status == "revoked"
    assert handles.remove_expired("handle-active", 30) == active
    assert handles.remove_expired("handle-active", 30) is None
    assert handles.expired(30) == (expired[1],)


def test_assignment_replacement_requires_live_admin_and_valid_profile_ids(tmp_path):
    service, users, _, assignments, _ = _service(
        tmp_path, valid_profiles={"profile-a", "profile-b"}
    )
    _add_admin(users)
    users.create("user-a", "member", hash_password("member password"))
    admin = _login(service)
    member = _login(service, "member", "member password")

    assert service.replace_assignments(admin.principal, "user-a", ["profile-a", "profile-b"]) == (
        "profile-a",
        "profile-b",
    )
    assert assignments.profiles_for_user("user-a") == ("profile-a", "profile-b")

    with pytest.raises(DomainError) as denied:
        service.replace_assignments(member.principal, "user-a", [])
    assert denied.value.code is ErrorCode.ADMIN_REQUIRED
    with pytest.raises(DomainError) as invalid:
        service.replace_assignments(admin.principal, "user-a", ["profile-a", "missing"])
    assert invalid.value.code is ErrorCode.FORBIDDEN_OR_NOT_FOUND
    with pytest.raises(DomainError) as duplicate:
        service.replace_assignments(admin.principal, "user-a", ["profile-a", "profile-a"])
    assert duplicate.value.code is ErrorCode.VALIDATION_ERROR
    assert assignments.profiles_for_user("user-a") == ("profile-a", "profile-b")


def test_local_bootstrap_is_single_use_and_returns_redacted_user(tmp_path):
    database_path = tmp_path / "bootstrap.sqlite3"
    user = initialize_admin(database_path, "local-admin", "first password")

    assert user == UserDTO(user.user_id, "local-admin", Role.ADMIN, True)
    stored = UserDAO(ExtensionDatabase(database_path)).get_by_username("local-admin")
    assert stored is not None and stored.password_hash.startswith("$argon2id$")
    assert "first password" not in repr(user)
    with pytest.raises(BootstrapAlreadyConfigured):
        initialize_admin(database_path, "replacement", "replacement password")
