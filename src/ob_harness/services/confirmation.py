"""Consume a real user confirmation and publish its exact candidate atomically."""

from datetime import datetime, timezone

from .. import __version__
from ..common import HarnessError, digest, now
from ..conversations.store import ConversationStore

CONFIRM_TEXT = {"确认保存", "/confirm", "confirm save"}


class ConfirmationService:
    def __init__(self, app):
        self.app, self.db, self.store = app, app.db, ConversationStore(app)

    def confirm(self, conversation_id, *, message, review_id, idempotency_key):
        self.store.get(conversation_id)
        if self.db.get("conversation_message", message["id"]) != message:
            raise HarnessError(
                "confirmation_required", "Confirmation must be a persisted user event"
            )
        if (
            message["role"] != "user"
            or message["conversation_id"] != conversation_id
            or message["text"].strip() not in CONFIRM_TEXT
        ):
            raise HarnessError(
                "confirmation_required", "A separate exact user confirmation is required"
            )
        key = digest([self.app.authorizer.principal.subject, conversation_id, idempotency_key])
        signature = digest([conversation_id, message["id"], review_id])
        with self.db.connect() as db:
            prior = db.execute(
                "SELECT 1 FROM objects WHERE kind='confirmation_receipt' AND id=?", (key,)
            ).fetchone()
            if prior:
                receipt = self.db.get("confirmation_receipt", key, db)
                if receipt["signature"] != signature:
                    raise HarnessError(
                        "idempotency_conflict", "Confirmation key has a different payload"
                    )
                return receipt["result"]
        packet = self.db.get("conversation_review", review_id)
        if (
            packet.get("validation_contract") != "ontology-edit-v2/1"
            or packet.get("package_version") != __version__
        ):
            raise HarnessError(
                "review_conflict", "Validation contract changed; prepare a new review"
            )
        value = self.app.ontologies.changeset(packet["changeset_id"])
        candidate, schema = self.app.ontologies.candidate_v2(value)
        verified = self.app.ontologies._snapshot(value["ontology_id"], candidate, schema)
        if verified["version"] != packet["candidate_hash"]:
            raise HarnessError(
                "review_conflict", "Revalidated candidate differs from the reviewed content"
            )
        with self.db.transaction() as db:
            conv = self.store.get(conversation_id, db)
            existing = db.execute(
                "SELECT 1 FROM objects WHERE kind='confirmation_receipt' AND id=?", (key,)
            ).fetchone()
            if existing:
                receipt = self.db.get("confirmation_receipt", key, db)
                if receipt["signature"] != signature:
                    raise HarnessError(
                        "idempotency_conflict", "Confirmation key has a different payload"
                    )
                return receipt["result"]
            active = (
                self.db.get("conversation_turn", conv["active_turn_id"], db)
                if conv["active_turn_id"]
                else None
            )
            if (
                not active
                or active["message_id"] != message["id"]
                or active["confirmation_review_id"] != review_id
            ):
                raise HarnessError(
                    "confirmation_required",
                    "This user message is not the active confirmation event",
                )
            packet = self.db.get("conversation_review", review_id, db)
            draft = self.store.draft(conv, db)
            if (
                packet["subject"] != conv["subject"]
                or packet["conversation_id"] != conversation_id
                or conv.get("pending_review_id") != review_id
                or packet["status"] != "pending"
                or not packet["presented"]
                or packet["draft_id"] != draft["id"]
                or packet["draft_revision"] != draft["revision"]
            ):
                raise HarnessError(
                    "review_conflict", "This review is no longer the active, presented draft"
                )
            if datetime.fromisoformat(packet["expires_at"]) <= datetime.now(timezone.utc):
                raise HarnessError(
                    "review_expired", "Review expired; request a fresh review before confirming"
                )
            # Immutable candidate was fully validated before review; its artifacts are
            # rechecked here. No nested write transactions or model calls in publish.
            content = self.db.read_artifact(packet["reference"]["content_hash"])
            if digest(content) != packet["candidate_hash"]:
                raise HarnessError("review_conflict", "Candidate checksum changed")
            committed = self.app.ontologies.publish_review(
                value, packet["reference"], review=packet, db=db
            )
            packet["status"] = "consumed"
            packet["confirmed_message_id"] = message["id"]
            draft["status"] = "committed"
            draft["committed_version"] = committed["version"]
            conv["pending_review_id"] = None
            result = {
                "kind": "ontology_committed",
                "version": committed["version"],
                "ontology_id": conv["ontology_id"],
                "review_id": review_id,
                "changeset_id": value["id"],
                "message": "已确认并保存正式本体版本。",
            }
            self.db.put("conversation_review", review_id, packet, db)
            self.db.put("draft", draft["id"], draft, db)
            self.store.save(conv, db)
            self.db.put(
                "confirmation_receipt",
                key,
                {
                    "signature": signature,
                    "user_message_id": message["id"],
                    "result": result,
                    "created_at": now(),
                },
                db,
            )
            self.db.audit(
                conv["subject"],
                "conversation_confirm",
                conv["ontology_id"],
                "allowed",
                {
                    "review_id": review_id,
                    "message_id": message["id"],
                    "version": committed["version"],
                },
                db,
            )
            return result
