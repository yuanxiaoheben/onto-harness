"""Small terminal adapter; all state and permissions live in the shared services."""

import json
from uuid import uuid4

from ..common import HarnessError
from ..services.application import Application


async def interactive_chat(args):
    service = Application(args.workspace).conversations
    conv = (
        service.get(args.conversation)
        if args.conversation
        else service.create(
            ontology_id=args.ontology, create_spec={} if args.new_ontology else None
        )
    )
    if conv["ontology_id"] != args.ontology:
        raise HarnessError("resource_not_bound", "Conversation belongs to another ontology")
    identifier = conv["id"]
    print(
        f"会话 {identifier}；/diff 查看差异，/undo 撤销，/save 审阅，确认保存，/resume 恢复，/exit 退出。",
        flush=True,
    )
    for message in service.messages(identifier)[-10:]:
        print(f"{message['role']}: {message['text']}", flush=True)
    if conv.get("pending_review_id"):
        review = service.db.get("conversation_review", conv["pending_review_id"])
        print(json.dumps(review["diff"], ensure_ascii=False, indent=2), flush=True)
        service.present(identifier, review["id"])
    while True:
        try:
            text = input("你> ").strip()
        except EOFError:
            return
        if text in {"/exit", "/quit"}:
            return
        if not text:
            continue
        try:
            conv = service.get(identifier)
            if text == "/resume":
                response = await service.resume(identifier)
            else:
                response = await service.send(
                    identifier,
                    text=text,
                    client_message_id=uuid4().hex,
                    expected_revision=conv["revision"],
                    planner=args.planner,
                )
            print(json.dumps(response, ensure_ascii=False, indent=2), flush=True)
            packet = response.get("result", {}).get("review")
            if packet:
                service.present(identifier, packet["id"])
        except HarnessError as error:
            print(json.dumps({"error": error.as_dict()}, ensure_ascii=False), flush=True)
