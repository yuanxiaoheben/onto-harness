"""Opt-in live conversation check on original toy ontology data in a fresh workspace."""

import argparse
import asyncio
import json
import os
from pathlib import Path
from uuid import uuid4

from ob_harness.common import safe_error
from ob_harness.services import Application
from ob_harness.services.application import initialize


async def check(args):
    if args.dotenv:
        for line in args.dotenv.read_text(encoding="utf-8-sig").splitlines():
            name, separator, value = line.partition("=")
            if separator and name.strip() == "DEEPSEEK_API_KEY":
                os.environ["DEEPSEEK_API_KEY"] = value.strip().strip('"').strip("'")
    if not os.environ.get("DEEPSEEK_API_KEY"):
        raise ValueError("Set DEEPSEEK_API_KEY or explicitly pass --dotenv")
    initialize(args.workspace, writable=True)
    path = args.workspace / "harness.json"
    config = json.loads(path.read_text(encoding="utf-8"))
    config["model"] = {
        "provider": "deepseek",
        "base_url": "https://api.deepseek.com",
        "model": args.model,
        "api_key_env": "DEEPSEEK_API_KEY",
        "timeout": 60,
        "retries": 0,
        "max_output_tokens": 2048,
        "json_mode": True,
        "thinking": {"type": "disabled"},
    }
    config["budget"] = {"max_model_calls": 2, "timeout_seconds": 120}
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    app = Application(args.workspace)
    service = app.conversations
    conv = service.create(ontology_id="demo", max_model_calls=16)
    identifier = conv["id"]
    original_version = app.ontologies.read("demo")["version"]
    report = {
        "workspace": str(args.workspace.resolve()),
        "model": args.model,
        "conversation_id": identifier,
        "steps": [],
        "passed": False,
    }

    def raw():
        return service.drafts.read(identifier)["ontology"]

    def concept(name):
        matches = [r for r in raw()["entities"] if r["name"] == name]
        if len(matches) != 1:
            raise AssertionError(f"Expected one concept named {name}")
        return matches[0]

    def expect(condition, label):
        if not condition:
            raise AssertionError(label)

    async def say(label, text, *, expected="completed", key=None, reply_to=None):
        current = service.get(identifier)
        response = await service.send(
            identifier,
            text=text,
            client_message_id=key or uuid4().hex,
            expected_revision=current["revision"],
            planner="llm",
            reply_to=reply_to,
        )
        step = {"label": label, "text": text, "response": response}
        report["steps"].append(step)
        print(
            json.dumps(
                {
                    "step": label,
                    "status": response["status"],
                    "draft_revision": response["draft"]["revision"],
                    "run_id": response["run_id"],
                    "error": response["result"].get("error"),
                },
                ensure_ascii=True,
            ),
            flush=True,
        )
        expect(response["status"] == expected, f"{label}: unexpected status {response['status']}")
        return response

    try:
        await say(
            "add_whale",
            "请在现有本体中新增“鲸”（类型 concept），定义为“生活在水中的哺乳动物”，并用 is_a 关系把鲸连接到已有的 Mammal；只使用本句给出的信息。",
        )
        whale_id = concept("鲸")["id"]
        expect(
            any(r["source"] == whale_id and r["target"] == "mammal" for r in raw()["relations"]),
            "Whale must be under Mammal",
        )
        await say("add_blue_whale", "再补充“蓝鲸”，定义为“一种鲸”，并将蓝鲸设为鲸的下位概念。")
        blue_id = concept("蓝鲸")["id"]
        expect(
            any(r["source"] == blue_id and r["target"] == whale_id for r in raw()["relations"]),
            "Blue whale must be under Whale",
        )
        await say(
            "correct_definition",
            "更正一下鲸的定义，请替换成“生活在水中的哺乳动物类群”。保留鲸的 ID、名称和现有层级关系。",
        )
        expect(
            concept("鲸")["definition"] == "生活在水中的哺乳动物类群",
            "Definition correction must be exact",
        )
        expect(concept("鲸")["id"] == whale_id, "Correction must preserve concept identity")
        await say("rename", "将蓝鲸重命名为“大蓝鲸”，保持定义及所有关系不变。")
        expect(concept("大蓝鲸")["id"] == blue_id, "Rename must preserve identity")
        await say("undo_rename", "撤销上一步")
        expect(concept("蓝鲸")["id"] == blue_id, "Undo must restore the original name")
        query = await say("draft_query", "请查询当前草案里鲸和蓝鲸的概念及已有层级关系。")
        expect(query["result"]["kind"] == "ontology_answer", "Query must return draft facts")
        before_delete = service.get(identifier)["draft"]["revision"]
        await say(
            "delete_scope_clarification",
            "请删除鲸，但不要自动删除或修改任何关联关系；如果存在关联关系，请先向我解释影响并确认范围。",
            expected="waiting_input",
        )
        expect(
            service.get(identifier)["draft"]["revision"] == before_delete,
            "Clarification must not mutate the draft",
        )
        await say(
            "delete_with_scope",
            "确认范围：删除鲸以及所有与鲸直接关联的 is_a 关系，保留蓝鲸、Mammal 和 Animal 这些概念。",
        )
        expect(
            whale_id not in {r["id"] for r in raw()["entities"]},
            "Deletion must remove the intended concept",
        )
        expect(concept("蓝鲸")["id"] == blue_id, "Deletion must keep child concepts")
        await say("undo_delete", "撤销上一步")
        expect(concept("鲸")["id"] == whale_id, "Undo deletion must restore the concept")
        expect(len(raw()["relations"]) == 3, "Undo deletion must restore all hierarchy edges")
        expect(
            app.ontologies.read("demo")["version"] == original_version,
            "Draft edits must not publish",
        )
        review = await say("review", "保存")
        packet = review["result"]["review"]
        expect(packet["diff"]["validation"]["passed"], "Review validation must pass")
        # This test runner is the transport: persist full review in report before
        # acknowledging presentation, then simulate an independent user event.
        (args.workspace / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        service.present(identifier, packet["id"])
        saved = await say("confirm", "确认保存", key="live-confirm", reply_to=packet["id"])
        expect(saved["result"]["kind"] == "ontology_committed", "Confirmation must publish")
        repeated = await say("confirm_retry", "确认保存", key="live-confirm", reply_to=packet["id"])
        expect(saved == repeated, "Confirmation retry must return the same receipt")
        expect(len(app.ontologies.versions("demo")) == 2, "Must publish exactly one new version")
        await say("query_after_commit", "请查询鲸和蓝鲸的概念记录及它们的层级关系。")
        final = app.ontologies.read("demo")
        report["final"] = {
            "version": final["version"],
            "concepts": final["ontology"]["entities"],
            "relations": final["ontology"]["relations"],
        }
        report["passed"] = True
    except Exception as error:
        report["check_error"] = safe_error(error)
        if isinstance(error, AssertionError):
            report["check_error"]["message"] = str(error)
    finally:
        run_ids = {
            step["response"]["run_id"] for step in report["steps"] if step["response"]["run_id"]
        }
        runs = [app.runs.get(run_id) for run_id in run_ids]
        report["model_calls"] = service.get(identifier)["model_calls"]
        report["usage"] = {
            key: sum(r["usage"][key] for r in runs)
            for key in ("input_tokens", "output_tokens", "unknown_calls")
        }
        (args.workspace / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    print(
        json.dumps(
            {
                key: report.get(key)
                for key in ("passed", "model_calls", "usage", "check_error", "workspace")
            },
            ensure_ascii=True,
        ),
        flush=True,
    )
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workspace",
        type=Path,
        required=True,
        help="New test workspace; existing files are never overwritten",
    )
    parser.add_argument("--model", required=True)
    parser.add_argument("--dotenv", type=Path)
    raise SystemExit(asyncio.run(check(parser.parse_args())))
