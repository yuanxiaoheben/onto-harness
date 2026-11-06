"""Trusted local identity and resource-level authorization."""

from dataclasses import dataclass
from typing import Any, Mapping

from ..common import HarnessError, digest
from ..state import Database


@dataclass(frozen=True)
class Principal:
    subject: str
    grants: Mapping[str, frozenset[str]]


class Authorizer:
    def __init__(self, database: Database, principal: Principal):
        self.db, self.principal = database, principal

    @property
    def fingerprint(self) -> str:
        return digest({key: sorted(value) for key, value in self.principal.grants.items()})

    def allowed(self, action: str, resource: str) -> bool:
        return action in self.principal.grants.get(resource, frozenset())

    def require(self, action: str, resource: str) -> None:
        if not self.allowed(action, resource):
            self.db.audit(self.principal.subject, action, resource, "denied")
            raise HarnessError(
                "permission_denied", f"Missing {action} permission on the selected resource"
            )

    def record(self, action: str, resource: str, details: dict[str, Any] | None = None) -> None:
        self.db.audit(self.principal.subject, action, resource, "allowed", details)
