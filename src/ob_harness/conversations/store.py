from typing import Any

from ..common import HarnessError


class ConversationStore:
    """Reuse transactional objects and content-addressed artifacts; no DDL migration."""

    def __init__(self, app):
        self.app, self.db = app, app.db

    def get(self, identifier: str, db=None) -> dict[str, Any]:
        value = self.db.get("conversation", identifier, db)
        if value["subject"] != self.app.authorizer.principal.subject:
            raise HarnessError("not_found", "Conversation not found")
        # Do not create an audit transaction while holding the write transaction.
        if not self.app.authorizer.allowed("read", value["ontology_id"]):
            raise HarnessError(
                "permission_denied", "Conversation requires ontology read permission"
            )
        return value

    def draft(self, conversation: dict, db=None) -> dict:
        return self.db.get("draft", conversation["active_draft_id"], db)

    def require_revision(self, conversation: dict, expected: int) -> None:
        if type(expected) is not int or conversation["revision"] != expected:
            raise HarnessError(
                "conversation_conflict", "Refresh the conversation before sending another message"
            )

    def save(self, conversation: dict, db) -> None:
        conversation["revision"] += 1
        self.db.put("conversation", conversation["id"], conversation, db)
