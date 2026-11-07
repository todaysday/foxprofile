"""Disposable acceptance data, a loopback input page, and explicit staging smoke checks.

No browser, target discovery, profile mutation, or lifecycle injection is performed.
"""

from __future__ import annotations

import argparse
import getpass
import ipaddress
import json
import os
import stat
import sys
import uuid
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx

CREDENTIAL_KEYS = ("admin0_password", "a_password", "b_password", "service_token")


def _no_symlinks(path: Path) -> Path:
    path = path.expanduser().absolute()
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError("symlink paths are not permitted")
    return path


def _private_json(path: Path) -> dict:
    path = _no_symlinks(path)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("private input must be a single-link regular file")
    if os.name == "posix" and (info.st_mode & 0o077 or info.st_uid != os.getuid()):
        raise ValueError("private input must be owned by this user with mode 0600")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("private input must contain a JSON object")
    return value


def credentials(path: Path | None) -> dict[str, str]:
    if path is None:
        if not sys.stdin.isatty():
            raise ValueError("provide a private credentials file or an interactive terminal")
        values = {key: getpass.getpass(f"{key}: ") for key in CREDENTIAL_KEYS}
    else:
        values = _private_json(path)
    if set(values) != set(CREDENTIAL_KEYS) or any(
        not isinstance(values[key], str) or not values[key] for key in CREDENTIAL_KEYS
    ):
        raise ValueError(
            "credentials require admin0_password, a_password, b_password, service_token"
        )
    for key in CREDENTIAL_KEYS[:-1]:
        if len(values[key].encode("utf-8")) > 1024:
            raise ValueError("password exceeds the supported byte limit")
    if len(values["service_token"]) < 24 or not values["service_token"].isascii():
        raise ValueError("service_token must contain at least 24 ASCII characters")
    return values


def _write_private(path: Path, value: dict) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        json.dump(value, output, indent=2)
        output.write("\n")


def prepare_fixture(destination: Path, secrets: dict[str, str]) -> dict:
    """Prepare only a new/empty explicit local directory, in a fresh CLI process."""
    if "src.core.config" in sys.modules:
        raise ValueError("prepare requires a fresh python -m foxprofile_ext.acceptance process")
    destination = _no_symlinks(destination)
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise ValueError("destination must be absent or an empty directory")
    destination.mkdir(mode=0o700, parents=False, exist_ok=True)
    destination.chmod(0o700)
    # A restrictive mask also covers catalog temporary files and profile directories.
    old_mask = os.umask(0o077)
    try:
        environment = {
            "FOXPROFILE_EXT_ENABLED": "true",
            "FOXPROFILE_EXT_DB": str(destination / "foxprofile_ext.sqlite3"),
            "FOXPROFILE_PROFILES_FILE": str(destination / "profiles.json"),
            "FOXPROFILE_DATA_DIR": str(destination / "browserdata"),
            "FOXPROFILE_LOG_DIR": str(destination / "logs"),
            "FOXPROFILE_API_HOST": "127.0.0.1",
            "FOXPROFILE_HEADLESS": "virtual",
            "FOXPROFILE_API_TOKEN": secrets["service_token"],
        }
        os.environ.update(environment)
        from src.services.profile.manager import ProfileManager

        from .auth import hash_password
        from .catalog import ProfileCatalog
        from .dto import Role
        from .storage.dao import AssignmentDAO, CatalogOperationDAO, SettingsDAO, UserDAO
        from .storage.database import ExtensionDatabase

        database = ExtensionDatabase(environment["FOXPROFILE_EXT_DB"])
        users = UserDAO(database)
        fixture_id = str(uuid.uuid4())
        namespace = uuid.UUID(fixture_id)
        accounts = {name: str(uuid.uuid5(namespace, name)) for name in ("admin0", "A", "B")}
        users.bootstrap_admin(
            accounts["admin0"], "admin0", hash_password(secrets["admin0_password"])
        )
        for name in ("A", "B"):
            users.create(
                accounts[name], name, hash_password(secrets[f"{name.lower()}_password"]), Role.USER
            )
        assignments = AssignmentDAO(database)
        catalog = ProfileCatalog(
            ProfileManager(),
            CatalogOperationDAO(database),
            assignments,
            destination / "profiles.lock",
        )
        profiles = {}
        for label in ("PA", "PB", *(f"P{index:02}" for index in range(3, 12))):
            owner = accounts.get({"PA": "A", "PB": "B"}.get(label, ""))
            profile = catalog.create(
                owner, name=f"accept-{fixture_id[:8]}-{label}", os_type="linux", engine="camoufox"
            )
            profiles[label] = {"profile_id": profile.profile_id, "name": profile.name}
        SettingsDAO(database).set_capacity(10)
        manifest = {
            "format": "foxprofile-acceptance-v1",
            "fixture_id": fixture_id,
            "users": accounts,
            "profiles": profiles,
            "capacity": 10,
        }
        _write_private(destination / "fixture.json", manifest)
        # Passwords stay exclusively in operator input. Service token is server configuration.
        _write_private(destination / "runtime-env.json", environment)
        return manifest
    finally:
        os.umask(old_mask)


class InputPageHandler(BaseHTTPRequestHandler):
    """Serve exactly one bundled page, never filesystem contents or request logs."""

    def do_GET(self) -> None:
        if self.path not in ("/", "/acceptance-input.html"):
            self.send_error(404)
            return
        body = Path(__file__).with_name("assets").joinpath("acceptance-input.html").read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'none'; form-action 'none'; base-uri 'none'; frame-ancestors 'none'",
        )
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:
        pass


def input_server(port: int) -> ThreadingHTTPServer:
    return ThreadingHTTPServer(("127.0.0.1", port), InputPageHandler)


class SmokeFailure(ValueError):
    """A named, secret-free smoke failure."""


def _expect(condition: bool, name: str) -> None:
    if not condition:
        raise SmokeFailure(name)


def _staging_origin(value: str) -> str:
    parts = urlsplit(value)
    try:
        local = ipaddress.ip_address(parts.hostname or "").is_loopback
    except ValueError:
        local = parts.hostname == "localhost"
    if (
        not parts.hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
        or parts.path not in ("", "/")
        or parts.scheme not in ("http", "https")
        or (parts.scheme == "http" and not local)
    ):
        raise ValueError("base URL must be an explicit HTTPS origin or loopback HTTP origin")
    return value.rstrip("/")


def smoke(fixture: dict, expected_id: str, base_url: str, secrets: dict[str, str]) -> list[str]:
    """Use real HTTP, no redirects/proxies, no browser or profile/settings mutation.

    Login/logout create/revoke only this command's personal sessions. Identity
    checks precede access checks; never use this as a production discovery tool.
    """
    _expect(fixture.get("format") == "foxprofile-acceptance-v1", "fixture format")
    _expect(fixture.get("fixture_id") == expected_id, "fixture identity confirmation")
    origin = _staging_origin(base_url)
    profile_ids = {item["profile_id"] for item in fixture["profiles"].values()}
    _expect(len(profile_ids) >= 11, "fixture requires eleven distinct profiles")
    passed = []
    with ExitStack() as stack:
        clients = {
            name: stack.enter_context(
                httpx.Client(base_url=origin, timeout=15, trust_env=False, follow_redirects=False)
            )
            for name in ("admin0", "A", "B", "service")
        }
        csrf = {}
        cleanup_failures = []
        try:
            for name in ("admin0", "A", "B"):
                key = "admin0_password" if name == "admin0" else f"{name.lower()}_password"
                response = clients[name].post(
                    "/api/v1/ext/auth/login",
                    json={"username": name, "password": secrets[key]},
                    headers={"Origin": origin},
                )
                _expect(response.status_code == 200, f"{name} personal login")
                result = response.json()
                csrf[name] = result["csrf_token"]
                _expect(result["user"]["user_id"] == fixture["users"][name], "target user identity")
                _expect(
                    result["user"]["role"] == ("admin" if name == "admin0" else "user"),
                    "target user role",
                )
            admin = clients["admin0"]
            response = admin.get("/api/v1/profiles")
            _expect(response.status_code == 200, "target catalog readable")
            actual = {item["profile_id"]: item["name"] for item in response.json()["profiles"]}
            expected = {item["profile_id"]: item["name"] for item in fixture["profiles"].values()}
            _expect(
                actual == expected,
                "target fixture catalog identity (exclusive disposable installation)",
            )
            passed.append("fixture identity and personal sessions")
            for name, own, foreign in (("A", "PA", "PB"), ("B", "PB", "PA")):
                client = clients[name]
                response = client.get("/api/v1/profiles")
                _expect(response.status_code == 200, f"{name} assigned catalog")
                _expect(
                    {item["profile_id"] for item in response.json()["profiles"]}
                    == {fixture["profiles"][own]["profile_id"]},
                    f"{name} assigned filtering",
                )
                own_id = fixture["profiles"][own]["profile_id"]
                own_response = client.get(f"/api/v1/profiles/{quote(f'id:{own_id}', safe='')}")
                _expect(own_response.status_code == 200, f"{name} canonical assigned detail")
                _expect(
                    own_response.json()["profile_id"] == own_id,
                    f"{name} canonical profile identity",
                )
                foreign_profile = fixture["profiles"][foreign]
                for identifier in (f"id:{foreign_profile['profile_id']}", foreign_profile["name"]):
                    _expect(
                        client.get(f"/api/v1/profiles/{quote(identifier, safe='')}").status_code
                        == 404,
                        f"{name} foreign denial",
                    )
                _expect(
                    client.get("/api/v1/ext/admin/settings").status_code == 403,
                    f"{name} settings denied",
                )
            passed.append("A/B filtering, foreign ID/name denial and settings boundary")
            service = clients["service"]
            service.headers["Authorization"] = f"Bearer {secrets['service_token']}"
            response = service.get("/api/v1/profiles")
            _expect(response.status_code == 200, "service catalog")
            _expect(
                {item["profile_id"] for item in response.json()["profiles"]} == profile_ids,
                "service catalog scope",
            )
            _expect(
                service.get("/api/v1/ext/auth/me").status_code == 404,
                "service is not a personal session",
            )
            _expect(
                service.get("/api/v1/ext/admin/users").status_code == 403,
                "service is not a personal administrator",
            )
            _expect(
                clients["A"]
                .get(
                    "/api/v1/profiles",
                    headers={
                        "Authorization": f"Bearer {clients['A'].cookies.get('foxprofile_session', '')}"
                    },
                )
                .status_code
                == 401,
                "personal cookie is not service bearer",
            )
            passed.append("service and personal session separation")
            response = admin.get("/api/v1/ext/admin/settings")
            _expect(response.status_code == 200, "admin capacity read")
            _expect(
                response.json()["max_concurrent_profiles"] == fixture["capacity"],
                "saved capacity value",
            )
            passed.append("administrator saved capacity read")
        finally:
            for name, token in csrf.items():
                try:
                    response = clients[name].post(
                        "/api/v1/ext/auth/logout",
                        headers={"Origin": origin, "X-CSRF-Token": token},
                    )
                except httpx.HTTPError:
                    cleanup_failures.append(f"{name} transport error")
                else:
                    if not 200 <= response.status_code < 300:
                        cleanup_failures.append(f"{name} HTTP {response.status_code}")
    _expect(not cleanup_failures, f"logout cleanup ({', '.join(cleanup_failures)})")
    return passed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare", help="create offline disposable fixture only")
    prepare.add_argument("--destination", required=True, type=Path)
    prepare.add_argument("--credentials-file", type=Path)
    page = commands.add_parser("serve-page", help="serve controlled page only on 127.0.0.1")
    page.add_argument("--port", default=8765, type=int)
    check = commands.add_parser("smoke", help="explicit staging HTTP checks; no browser actions")
    check.add_argument("--fixture", required=True, type=Path)
    check.add_argument("--expect-fixture", required=True)
    check.add_argument("--base-url", required=True)
    check.add_argument("--credentials-file", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            manifest = prepare_fixture(args.destination, credentials(args.credentials_file))
            print(f"Prepared disposable fixture {manifest['fixture_id']}; no target contacted.")
        elif args.command == "serve-page":
            with input_server(args.port) as server:
                print(f"Controlled input page: http://127.0.0.1:{server.server_port}/", flush=True)
                server.serve_forever()
        else:
            fixture = _private_json(args.fixture / "fixture.json")
            for name in smoke(
                fixture, args.expect_fixture, args.base_url, credentials(args.credentials_file)
            ):
                print(f"PASS {name}")
            print("Browser/noVNC/source acceptance: NOT RUN by this command.")
    except SmokeFailure as error:
        print(f"FAIL {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    except (ValueError, OSError, httpx.HTTPError, KeyError, TypeError):
        # Never echo inputs, responses, request URLs, or credentials through exception text.
        print(
            "Acceptance command failed; check private inputs, fixture identity and staging setup.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
