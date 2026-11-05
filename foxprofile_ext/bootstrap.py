"""One-time, interactive local administrator initialization command."""

from __future__ import annotations

import argparse
import getpass
import secrets
import sys
from pathlib import Path

from .auth import UserDTO, _validate_username, hash_password
from .dto import Role
from .errors import DomainError
from .storage.dao import BootstrapAlreadyConfigured, UserDAO
from .storage.database import ExtensionDatabase


def initialize_admin(database_path: str | Path, username: str, password: str) -> UserDTO:
    """Create the first administrator in the explicitly selected local database."""
    normalized = _validate_username(username)
    users = UserDAO(ExtensionDatabase(database_path))
    account = users.bootstrap_admin(secrets.token_urlsafe(18), normalized, hash_password(password))
    return UserDTO(account.user_id, account.username, Role.ADMIN, account.active)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Initialize FoxProfile's first administrator. Use the same extension database "
            "path configured for the running installation."
        )
    )
    parser.add_argument(
        "--database",
        required=True,
        type=Path,
        help="explicit path to this installation's extension SQLite database",
    )
    parser.add_argument("--username", help="account name; prompts when omitted")
    args = parser.parse_args(argv)
    if not sys.stdin.isatty():
        parser.error("bootstrap requires an interactive local terminal")

    username = args.username if args.username is not None else input("Administrator username: ")
    password = getpass.getpass("Administrator password: ")
    confirmation = getpass.getpass("Confirm administrator password: ")
    if password != confirmation:
        parser.error("passwords do not match")

    try:
        account = initialize_admin(args.database, username, password)
    except BootstrapAlreadyConfigured:
        parser.error("administrator bootstrap is already complete")
    except DomainError as error:
        parser.error(error.message)
    print(f"Initialized administrator: {account.username}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
