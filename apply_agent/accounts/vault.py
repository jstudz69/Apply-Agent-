"""Per-tenant credentials. Every Workday company is its own tenant with its own account.

Secrets live in the OS keychain via `keyring` (macOS Keychain, Windows Credential Locker,
Secret Service on Linux). Swap `KeyringVault` for a 1Password/Bitwarden CLI wrapper if you
prefer; the driver only needs get/put.
"""

from __future__ import annotations

import json
import secrets
import string
from dataclasses import asdict, dataclass
from typing import Protocol

SERVICE = "apply-agent"


@dataclass
class Credential:
    email: str
    password: str
    verified: bool = False


class Vault(Protocol):
    def get(self, account_key: str) -> Credential | None: ...
    def put(self, account_key: str, cred: Credential) -> None: ...


class KeyringVault:
    def get(self, account_key: str) -> Credential | None:
        import keyring

        raw = keyring.get_password(SERVICE, account_key)
        return Credential(**json.loads(raw)) if raw else None

    def put(self, account_key: str, cred: Credential) -> None:
        import keyring

        keyring.set_password(SERVICE, account_key, json.dumps(asdict(cred)))


class MemoryVault:
    """For tests and dry runs."""

    def __init__(self) -> None:
        self._store: dict[str, Credential] = {}

    def get(self, account_key: str) -> Credential | None:
        return self._store.get(account_key)

    def put(self, account_key: str, cred: Credential) -> None:
        self._store[account_key] = cred


def generate_password(length: int = 20) -> str:
    """Satisfies every Workday password policy seen in practice: upper, lower, digit,
    special, length >= 8. Avoids characters some tenants reject (quotes, backslash, <>)."""
    specials = "!@#$%^&*-_=+?"
    alphabet = string.ascii_letters + string.digits + specials
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(length))
        if (any(c.islower() for c in pw) and any(c.isupper() for c in pw)
                and any(c.isdigit() for c in pw) and any(c in specials for c in pw)):
            return pw
