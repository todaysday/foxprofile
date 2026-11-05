"""Pure extension contracts: no application startup or browser fixtures."""

import json
import subprocess
import sys
from dataclasses import FrozenInstanceError, asdict, replace
from datetime import datetime, timezone

import pytest

from foxprofile_ext.dto import (
    BrowserRunRefDTO,
    CapacitySettings,
    ManualSessionDTO,
    ManualSessionState,
    PrincipalDTO,
    PrincipalKind,
    ProcessIdentity,
    ProfileRefDTO,
    Role,
    RuntimeDescriptor,
)
from foxprofile_ext.errors import DomainError, ErrorCode, ProfileCapacityExceeded
from foxprofile_ext.policy import (
    require_admin,
    require_authenticated,
    require_capacity,
    require_personal,
    require_profile_access,
    require_service,
)

ADMIN = PrincipalDTO("admin-0", PrincipalKind.PERSONAL, Role.ADMIN, "login-admin")
USER_A = PrincipalDTO("user-a", PrincipalKind.PERSONAL, Role.USER, "login-a")
USER_B = PrincipalDTO("user-b", PrincipalKind.PERSONAL, Role.USER, "login-b")
SERVICE = PrincipalDTO("trusted-automation", PrincipalKind.SERVICE)
PROFILE = ProfileRefDTO("profile-a", "Example profile")
RUN = BrowserRunRefDTO("profile-a", "run-a")
WEBSOCKET_PATH = "/api/v1/ext/manual-sessions/manual-a/ws"


def test_contracts_import_without_site_packages_or_application_dependencies():
    subprocess.run(
        [
            sys.executable,
            "-S",
            "-c",
            "import sys; from foxprofile_ext import dto, errors, policy; "
            "assert not {'fastapi', 'flet', 'sqlite3', 'camoufox', 'playwright', 'src'} "
            "& sys.modules.keys()",
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def test_personal_roles_and_service_identity_are_separate():
    assert {role.value for role in Role} == {"admin", "user"}
    assert SERVICE.role is None
    assert SERVICE.session_id is None
    assert require_personal(ADMIN) is ADMIN
    assert require_service(SERVICE) is SERVICE
    assert PrincipalDTO("user-a", "personal", "user", "login-a").role is Role.USER


@pytest.mark.parametrize("role", ["service", "owner", "ADMIN", "", None, True])
def test_invalid_personal_roles_are_rejected(role):
    with pytest.raises(DomainError) as error:
        PrincipalDTO("user-a", PrincipalKind.PERSONAL, role, "login-a")
    assert error.value.code is ErrorCode.VALIDATION_ERROR


@pytest.mark.parametrize(
    ("kind", "role", "session_id"),
    [
        ("unknown", None, None),
        ("service", "admin", None),
        ("service", None, "login-a"),
        ("personal", "user", None),
    ],
)
def test_principal_cannot_mix_authentication_kinds(kind, role, session_id):
    with pytest.raises(DomainError) as error:
        PrincipalDTO("example-subject", kind, role, session_id)
    assert error.value.code is ErrorCode.VALIDATION_ERROR


@pytest.mark.parametrize("principal", [None, {}, {"role": "admin"}, object()])
def test_missing_or_unknown_authority_never_becomes_service_access(principal):
    with pytest.raises(DomainError) as error:
        require_authenticated(principal)
    assert error.value.code is ErrorCode.AUTH_REQUIRED


@pytest.mark.parametrize("principal", [USER_A, SERVICE])
def test_only_personal_administrators_can_administer(principal):
    assert require_admin(ADMIN) is ADMIN
    with pytest.raises(DomainError) as error:
        require_admin(principal)
    assert error.value.code is ErrorCode.ADMIN_REQUIRED


@pytest.mark.parametrize(
    ("check", "principal"),
    [(require_personal, SERVICE), (require_service, ADMIN), (require_service, USER_A)],
)
def test_authentication_kinds_cannot_substitute_for_each_other(check, principal):
    with pytest.raises(DomainError) as error:
        check(principal)
    assert error.value.code is ErrorCode.FORBIDDEN_OR_NOT_FOUND


@pytest.mark.parametrize("principal", [ADMIN, USER_A, SERVICE])
def test_profile_access_uses_full_grants_or_explicit_privileged_identity(principal):
    assignments = {("user-a", "profile-a")}
    assert (
        require_profile_access(
            principal, PROFILE, lambda user, profile: (user, profile) in assignments
        )
        is PROFILE
    )


def test_foreign_and_missing_profiles_have_the_same_redacted_error():
    errors = []
    for profile in (PROFILE, None):
        with pytest.raises(DomainError) as error:
            require_profile_access(USER_B, profile, lambda user, profile: False)
        errors.append(error.value)
    assert errors[0].code is errors[1].code is ErrorCode.FORBIDDEN_OR_NOT_FOUND
    assert str(errors[0]) == str(errors[1])
    assert PROFILE.name not in str(errors[0])
    assert PROFILE.profile_id not in str(errors[0])


def test_profile_access_checks_authentication_before_resource_state():
    with pytest.raises(DomainError) as error:
        require_profile_access(None, None, lambda user, profile: True)
    assert error.value.code is ErrorCode.AUTH_REQUIRED


def test_renaming_and_reusing_names_do_not_transfer_grants():
    assignments = {("user-a", "profile-a")}

    def assigned(user_id, profile_id):
        return (user_id, profile_id) in assignments

    renamed = replace(PROFILE, name="Renamed example")
    assert require_profile_access(USER_A, renamed, assigned) is renamed
    with pytest.raises(DomainError):
        require_profile_access(USER_A, ProfileRefDTO("profile-b", PROFILE.name), assigned)
    assignments.clear()
    with pytest.raises(DomainError):
        require_profile_access(USER_A, renamed, assigned)


def test_profile_and_run_identity_are_immutable():
    with pytest.raises(FrozenInstanceError):
        PROFILE.profile_id = "replacement"
    with pytest.raises(FrozenInstanceError):
        RUN.run_id = "replacement"
    assert replace(RUN, run_id="run-b") != RUN


@pytest.mark.parametrize("invalid", ["", " ", None, 42])
def test_empty_or_non_string_identity_is_rejected(invalid):
    with pytest.raises(DomainError) as error:
        ProfileRefDTO(invalid, "Example")
    assert error.value.code is ErrorCode.VALIDATION_ERROR


def test_public_manual_session_excludes_runtime_and_personal_session_details():
    runtime = RuntimeDescriptor(
        RUN,
        "installation-1",
        ProcessIdentity(101, 3000),
        browser=ProcessIdentity(102, 3001),
        display=":101",
        namespace="private-namespace",
        tmp_path="/private/run-a",
        xauthority_path="/private/run-a/Xauthority",
        control_port=12345,
        control_token="example-private-control-token",
    )
    public = asdict(
        ManualSessionDTO("manual-a", RUN.profile_id, RUN.run_id, "active", WEBSOCKET_PATH)
    )
    assert public == {
        "session_id": "manual-a",
        "profile_id": "profile-a",
        "browser_run_id": "run-a",
        "state": "active",
        "websocket_path": WEBSOCKET_PATH,
        "expires_at": None,
    }
    assert json.loads(json.dumps(public)) == public
    assert "private" not in repr(runtime)
    assert "login-a" not in json.dumps(public)


@pytest.mark.parametrize("state", list(ManualSessionState))
def test_only_closed_manual_lease_frees_the_slot(state):
    assert state.occupies_slot is (state is not ManualSessionState.CLOSED)


def test_manual_state_and_runtime_target_contracts_are_validated():
    with pytest.raises(DomainError):
        ManualSessionDTO("manual-a", RUN.profile_id, RUN.run_id, "available", WEBSOCKET_PATH)
    with pytest.raises(DomainError):
        RuntimeDescriptor(RUN, "installation-1", ProcessIdentity(101, 3000), control_port=12345)
    with pytest.raises(TypeError):
        ManualSessionDTO(
            "manual-a",
            RUN.profile_id,
            RUN.run_id,
            "active",
            WEBSOCKET_PATH,
            backend_token="example",
        )


@pytest.mark.parametrize(
    "path",
    [
        "https://example.invalid/ws",
        "//example.invalid/ws",
        "/ws?token=example",
        "/\\other/ws",
        "/\x00ws",
        "/ws fragment",
    ],
)
def test_manual_websocket_reference_is_an_origin_local_path_without_credentials(path):
    with pytest.raises(DomainError) as error:
        ManualSessionDTO("manual-a", RUN.profile_id, RUN.run_id, "active", path)
    assert error.value.code is ErrorCode.VALIDATION_ERROR


def test_manual_expiry_requires_an_explicit_timezone():
    with pytest.raises(DomainError):
        ManualSessionDTO(
            "manual-a", RUN.profile_id, RUN.run_id, "active", WEBSOCKET_PATH, datetime(2030, 1, 1)
        )
    expiry = datetime(2030, 1, 1, tzinfo=timezone.utc)
    response = ManualSessionDTO(
        "manual-a", RUN.profile_id, RUN.run_id, "active", WEBSOCKET_PATH, expiry
    )
    assert response.expires_at == expiry


@pytest.mark.parametrize(("pid", "start_ticks"), [(0, 1), (True, 1), (1, -1), (1, 1.5)])
def test_process_identity_requires_pid_and_start_time(pid, start_ticks):
    with pytest.raises(DomainError) as error:
        ProcessIdentity(pid, start_ticks)
    assert error.value.code is ErrorCode.VALIDATION_ERROR


@pytest.mark.parametrize("invalid", [0, -1, 1.5, 10.0, True, False, "10", None])
def test_capacity_setting_is_a_strict_positive_integer(invalid):
    with pytest.raises(DomainError) as error:
        CapacitySettings(invalid)
    assert error.value.code is ErrorCode.VALIDATION_ERROR


def test_capacity_default_admission_and_lowered_limit():
    assert CapacitySettings().max_concurrent_profiles == 10
    require_capacity(CapacitySettings(12), occupied=10, requested=2)
    require_capacity(CapacitySettings(6), occupied=10, requested=0)
    with pytest.raises(ProfileCapacityExceeded) as error:
        require_capacity(CapacitySettings(6), occupied=10, requested=1)
    assert error.value.code is ErrorCode.PROFILE_CAPACITY_EXCEEDED
    assert error.value.max_concurrent_profiles == 6
    assert "6" in str(error.value)


@pytest.mark.parametrize("principal", [ADMIN, USER_A, SERVICE])
def test_authority_does_not_exempt_a_batch_from_capacity(principal):
    require_profile_access(principal, PROFILE, lambda user, profile: True)
    with pytest.raises(ProfileCapacityExceeded):
        require_capacity(CapacitySettings(10), occupied=9, requested=2)


@pytest.mark.parametrize(("occupied", "requested"), [(-1, 1), (True, 1), (0, -1), (0, 1.5)])
def test_capacity_rejects_invalid_counts(occupied, requested):
    with pytest.raises(DomainError) as error:
        require_capacity(CapacitySettings(), occupied, requested)
    assert error.value.code is ErrorCode.VALIDATION_ERROR


def test_machine_error_codes_match_the_api_contract():
    assert {code.value for code in ErrorCode} == {
        "AUTH_REQUIRED",
        "FORBIDDEN_OR_NOT_FOUND",
        "ADMIN_REQUIRED",
        "VALIDATION_ERROR",
        "PROFILE_CAPACITY_EXCEEDED",
        "PROFILE_BUSY",
        "MANUAL_SESSION_BUSY",
        "MANUAL_UNSUPPORTED_RUNTIME",
        "STALE_BROWSER_RUN",
        "MANUAL_SESSION_CLOSED",
        "RUNTIME_RECOVERING",
    }
