import argparse
import getpass
from collections.abc import Callable, Sequence

from secagent.config import Settings, get_settings
from secagent.db import make_session_factory
from secagent.domain import UserRole
from secagent.services.auth_service import AuthService, UserAlreadyExistsError


def main(
    argv: Sequence[str] | None = None,
    *,
    settings: Settings | None = None,
    password_reader: Callable[[str], str] = getpass.getpass,
) -> int:
    parser = argparse.ArgumentParser(prog="python -m secagent.cli")
    commands = parser.add_subparsers(dest="command", required=True)
    create_admin = commands.add_parser("create-admin")
    create_admin.add_argument("--username", required=True)
    args = parser.parse_args(argv)

    resolved = settings or get_settings()
    password = password_reader("Password: ")
    confirmation = password_reader("Confirm password: ")
    if password != confirmation:
        parser.error("passwords do not match")

    factory = make_session_factory(resolved.database_url)
    with factory() as session:
        try:
            AuthService.from_session(session, resolved).create_user(
                args.username, password, UserRole.ADMIN
            )
        except UserAlreadyExistsError:
            parser.error("username already exists")
    print(f"Created administrator {args.username}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
