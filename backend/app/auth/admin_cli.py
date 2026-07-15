from __future__ import annotations

import argparse
import getpass
import sys
from collections.abc import Callable, Sequence
from typing import Any

from ..core.config import get_settings
from ..storage.db import DatabaseConnectionManager
from .service import (
    UserRoleNotFound,
    change_user_role,
    get_user_by_username,
    list_user_role_audit,
    migrate_auth_schema,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="管理旅行助手用户的管理员角色")
    commands = parser.add_subparsers(dest="command", required=True)
    for command, help_text in (
        ("promote", "将用户提升为管理员"),
        ("demote", "将管理员降为普通用户"),
        ("show", "查看用户当前角色与最近变更"),
    ):
        subparser = commands.add_parser(command, help=help_text)
        subparser.add_argument("username", help="用户名")
    return parser


def _show_user(manager: DatabaseConnectionManager, username: str) -> None:
    user = get_user_by_username(manager, username)
    if user is None:
        raise UserRoleNotFound(username)

    print(f"用户名: {user['username']}")
    print(f"用户ID: {user['id']}")
    print(f"角色: {user['role']}")
    print("最近角色变更:")
    audit_rows = list_user_role_audit(manager, str(user["id"]))
    if not audit_rows:
        print("  无")
        return
    for row in audit_rows:
        changed_at = row.get("changed_at")
        formatted_time = changed_at.isoformat() if hasattr(changed_at, "isoformat") else str(changed_at)
        print(
            "  "
            f"{formatted_time} {row['previous_role']} -> {row['new_role']} "
            f"操作人: {row['changed_by']}"
        )


def main(
    argv: Sequence[str] | None = None,
    *,
    settings_loader: Callable[[], Any] = get_settings,
    manager_factory: Callable[[str], DatabaseConnectionManager] = DatabaseConnectionManager,
    actor_loader: Callable[[], str] = getpass.getuser,
) -> int:
    args = _parser().parse_args(argv)
    settings = settings_loader()
    database_url = getattr(settings, "database_url", None)
    if not database_url:
        print("DATABASE_URL 未配置，无法管理用户角色", file=sys.stderr)
        return 2

    manager: DatabaseConnectionManager | None = None
    try:
        manager = manager_factory(database_url)
        migrate_auth_schema(manager)
        if args.command == "show":
            _show_user(manager, args.username)
            return 0

        new_role = "admin" if args.command == "promote" else "user"
        result = change_user_role(
            manager,
            args.username,
            new_role,
            changed_by=actor_loader(),
        )
        if result.changed:
            print(f"{result.username}: {result.previous_role} -> {result.new_role}")
        else:
            print(f"{result.username}: 已是 {result.new_role}")
        return 0
    except UserRoleNotFound:
        print(f"用户不存在: {args.username}", file=sys.stderr)
        return 2
    except Exception:
        print("角色管理失败，请检查数据库配置和服务状态", file=sys.stderr)
        return 1
    finally:
        if manager is not None:
            manager.close()


if __name__ == "__main__":
    raise SystemExit(main())
