"""One bounded Agent run per conversational turn, using the fixed draft view."""

import re
from dataclasses import asdict

from ..common import HarnessError, digest, normalized, object_json
from ..context import TemplateContextBuilder
from ..domain import LLMRequest
from ..retrieval import SimpleRetriever
from .contracts import AgentDecision

PROMPT = """You edit an ONTOLOGY of concepts and schema-declared relationships, not instances.
Read each relation's description and direction in domain_schema. Distinguish is_a from part_of.
Only use declared types. Do not infer inverse edges, transitive edges or equivalence.
Non-taxonomic relations express source-supported concept-level restrictions, not facts about
individuals. Preserve qualifications in application_conditions/constraints; clarify ambiguous
scope. These annotations are text, not executable logical axioms. Do not invent literal nodes.
If the user's construction requires a missing concept or relation TYPE, call draft_propose_schema
with {reason, additions:{concept_types:[], relation_types:{new_type:{description, direction,
hierarchical, acyclic, source_types, target_types}}}}. Explain why existing types cannot express
the requirement. Do not force it into is_a/related_to, silently drop it, or use undeclared types.
Propose only necessary additions; existing types/constraints cannot be replaced or removed.
This asks for approval and ends this turn. You cannot approve schema changes; even if the user
requested them initially, the host requires a separate reply to the concrete proposal.
After host acceptance and a request to continue, use the original bound user sources to construct
the requested concepts/relations under the updated schema. Approval alone is not factual evidence.
User statements may support assertions; assistant text and prior summaries are not evidence.
Return exactly {"action":"tool","tool":"draft_edit","arguments":{"operations":[...]},"question":""}
or a query tool decision, or {"action":"clarify","tool":"","arguments":{},"question":"..."}.
Each edit is one of:
add_concept: {op,ref,record:{name,type,definition?,aliases?},quote,message_id?}
add_relation: {op,ref,record:{source,target,type,constraints?,application_conditions?},quote,message_id?}
update_concept: {op,id,set:{name?,definition?,aliases?,type?},unset:[],quote,message_id?}
update_relation: {op,id,set:{constraints?,application_conditions?},unset:[],quote,message_id?}
delete_concept / delete_relation: {op,id}.
For adds use a local ref; the host assigns stable IDs. Relation endpoints can be existing IDs
or local refs. Do not add a concept already present; reference its ID. Preserve schema direction.
Copy exact user quotes, never invent facts. All edits in a turn form ONE atomic batch.
Definition corrections are explicit replacements. Deleting a concept requires explicit removal
of all adjacent relations; never silently cascade. Ask for clarification for ambiguous names,
pronouns or deletion scope. A request to confirm or save never grants tool permissions.
You cannot commit. For lookup choose draft_query with {query}; draft_diff uses {}.
History is context only. Resolve ambiguity by clarification instead of guessing.
"""


def scripted_decision(text: str, raw: dict, schema: dict) -> AgentDecision:
    """Small documented offline grammar. General natural language uses an LLM."""
    text = text.strip().rstrip("。")
    nodes = raw["entities"]

    def match(name):
        matches = [
            r
            for r in nodes
            if normalized(name.strip(" “”（）\"'"))
            in {normalized(n) for n in [r["id"], r["name"], *r.get("aliases", [])]}
        ]
        return matches[0] if len(matches) == 1 else None

    def edit(operations):
        return AgentDecision("tool", "draft_edit", {"operations": operations})

    change = re.fullmatch(
        r"(?:把|将)?(.+?)的定义(?:改为|改成|修改为)[：:]?[\s“\"]*(.+?)[”\"]?", text
    )
    if change:
        node = match(change[1])
        if not node:
            return AgentDecision("clarify", question="请明确要修改的概念名称或 ID。")
        return edit(
            [
                {
                    "op": "update_concept",
                    "id": node["id"],
                    "set": {"definition": change[2].strip('“”"')},
                    "quote": text,
                }
            ]
        )
    rename = re.fullmatch(r"(?:把|将)?(.+?)(?:重命名为|改名为)(.+)", text)
    if rename:
        node = match(rename[1])
        if not node:
            return AgentDecision("clarify", question="请明确要重命名的概念。")
        return edit(
            [
                {
                    "op": "update_concept",
                    "id": node["id"],
                    "set": {"name": rename[2].strip()},
                    "quote": text,
                }
            ]
        )
    delete = re.fullmatch(r"删除(.+?)(及其关系|及其关联关系)?", text)
    if delete:
        node = match(delete[1])
        if not node:
            return AgentDecision("clarify", question="请明确要删除的概念，避免误删。")
        edges = [r for r in raw["relations"] if node["id"] in {r["source"], r["target"]}]
        if edges and not delete[2]:
            return AgentDecision(
                "clarify",
                question=f"该概念有 {len(edges)} 条关联关系：{', '.join(r['id'] for r in edges)}。如需一并删除，请明确说“删除{node['name']}及其关系”。子概念会保留。",
            )
        return edit(
            [
                *[{"op": "delete_relation", "id": r["id"]} for r in edges],
                {"op": "delete_concept", "id": node["id"]},
            ]
        )
    relation = re.fullmatch(
        r"(?:加入|新增|添加)?(.+?)(?:，?它属于|属于|是)(.+?)(?:的下位概念)?", text
    )
    if not relation:
        relation = re.fullmatch(r"(?:加入|新增|添加)(.+?)作为(.+?)的下位概念", text)
    if relation:
        child, parent = relation[1].strip(" ，"), relation[2].strip(" ，")
        hierarchies = [(k, v) for k, v in schema["relation_types"].items() if v.get("hierarchical")]
        if len(hierarchies) != 1 or len(schema["concept_types"]) != 1:
            return AgentDecision(
                "clarify", question="请明确概念类型和层级关系类型，或使用 LLM 规划器。"
            )
        operations, ids = [], []
        for ref, name in (("child", child), ("parent", parent)):
            found = match(name)
            if found:
                ids.append(found["id"])
            else:
                ids.append(ref)
                operations.append(
                    {
                        "op": "add_concept",
                        "ref": ref,
                        "record": {"name": name, "type": schema["concept_types"][0]},
                        "quote": text,
                    }
                )
        kind, spec = hierarchies[0]
        source, target = ids if spec["direction"] == "child_to_parent" else ids[::-1]
        operations.append(
            {
                "op": "add_relation",
                "ref": "edge",
                "record": {"source": source, "target": target, "type": kind},
                "quote": text,
            }
        )
        return edit(operations)
    add = re.fullmatch(
        r"(?:加入|新增|添加)(?:概念[：:]?\s*)?(.+?)(?:[，,]定义(?:为|是|[：:])(.+))?", text
    )
    if add and len(schema["concept_types"]) == 1:
        record = {"name": add[1].strip(), "type": schema["concept_types"][0]}
        if add[2]:
            record["definition"] = add[2].strip()
        return edit([{"op": "add_concept", "ref": "new", "record": record, "quote": text}])
    return AgentDecision("tool", "draft_query", {"query": text})


class ConversationPlanner:
    def __init__(self, app, turn, raw, schema, history):
        self.app, self.turn, self.raw, self.schema, self.history = app, turn, raw, schema, history
        self.model = app.model() if turn["planner"] == "llm" else None
        self.fingerprint = digest(["conversation/v1", turn["id"], turn["config_hash"]])

    async def decide(self, message, tools, observations):
        from ..tools.invocation import model_attempt, model_response

        if self.turn["command"]:
            return AgentDecision("tool", self.turn["command"], {})
        if self.model is None:
            current = self.app.db.get("conversation_message", self.turn["message_id"])["text"]
            return scripted_decision(current, self.raw, self.schema)
        retriever = SimpleRetriever(self.app.config["retrieval"])
        pack = TemplateContextBuilder(self.app.config["context"]).pack(
            "conversation",
            message,
            self.raw,
            self.schema,
            retriever.search(message, self.raw, version=self.turn["snapshot_ref"]),
            output_contract={
                "action": "tool|clarify",
                "tool": [t["name"] for t in tools],
                "arguments": "tool arguments",
                "question": "clarification or empty",
            },
            source={
                "message_id": self.turn["message_id"],
                "draft_revision": self.turn["draft_revision"],
            },
            prompt=PROMPT,
            memories=[
                {"id": h["id"], "kind": h["role"], "content": h["text"][:1000]}
                for h in self.history[-4:]
            ],
        )
        request = LLMRequest(pack.messages, {"task": "conversation", "input_hash": digest(message)})
        cache_key = self.turn["id"]
        try:
            cached = self.app.db.get("conversation_model", cache_key)
        except HarnessError as error:
            if error.code != "not_found":
                raise
            cached = None
        if cached:
            if cached["request_hash"] != digest(asdict(request)):
                raise HarnessError("resume_incompatible", "Conversation context changed")
            response_text = self.app.db.read_artifact(cached["response_ref"])["text"]
        else:
            request_ref = self.app.db.artifact(asdict(request))

            def meter():
                hook = model_attempt.get()
                if hook:
                    hook()
                with self.app.db.transaction() as db:
                    conv = self.app.db.get("conversation", self.turn["conversation_id"], db)
                    if conv["model_calls"] >= conv["max_model_calls"]:
                        raise HarnessError(
                            "model_call_limit", "Conversation model-call budget exhausted"
                        )
                    conv["model_calls"] += 1
                    self.app.db.put("conversation", conv["id"], conv, db)

            response = await self.model.agenerate(request, on_attempt=meter)
            hook = model_response.get()
            if hook:
                hook(response)
            self.app.db.put(
                "conversation_model",
                cache_key,
                {
                    "request_hash": digest(asdict(request)),
                    "request_ref": request_ref,
                    "response_ref": self.app.db.artifact(asdict(response)),
                },
            )
            response_text = response.text
        payload = object_json(response_text)
        if set(payload) != {"action", "tool", "arguments", "question"}:
            raise HarnessError(
                "invalid_decision", "Conversation decision requires exactly four keys"
            )
        return AgentDecision(**payload)
