"""Explicit Telegram allowlist, kept separately from the financial ledger."""
from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Mapping


def _parse_id_list(value: str, name: str) -> frozenset[int]:
    """Parse comma-separated Telegram IDs, rejecting malformed or nonpositive IDs."""
    if not value.strip():
        return frozenset()
    result: set[int] = set()
    for part in value.split(","):
        item = part.strip()
        if not item or not item.isascii() or not item.isdecimal() or int(item) <= 0:
            raise ValueError(f"{name}: укажи Telegram ID через запятую, например 123456789,987654321")
        result.add(int(item))
    return frozenset(result)


@dataclass(frozen=True)
class AccessPolicy:
    """Admins are automatically members of the allowlist."""

    users: frozenset[int]
    admins: frozenset[int]

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "AccessPolicy":
        admins = _parse_id_list(env.get("ADMIN_USER_IDS", ""), "ADMIN_USER_IDS")
        users = _parse_id_list(env.get("ALLOWED_USER_IDS", ""), "ALLOWED_USER_IDS")
        if not admins:
            raise ValueError("ADMIN_USER_IDS обязателен: добавь хотя бы один Telegram ID администратора в .env")
        return cls(users=users, admins=admins)

    def is_admin(self, user_id: int) -> bool:
        return user_id in self.admins

    def is_allowed(self, user_id: int) -> bool:
        return user_id in self.users or self.is_admin(user_id)

    @property
    def all_user_ids(self) -> tuple[int, ...]:
        return tuple(sorted(self.users | self.admins))

    @property
    def notification_admin_id(self) -> int:
        """Send backup failure notices to one stable admin, not everyone."""
        return min(self.admins)


def may_dispatch_message(policy: AccessPolicy, user_id: int, text: str | None) -> bool:
    """Anyone may request only their own /id; every other update needs allowlisting."""
    parts = (text or "").strip().split(maxsplit=1)
    command = parts[0].split("@", 1)[0] if parts else ""
    return command == "/id" or policy.is_allowed(user_id)
