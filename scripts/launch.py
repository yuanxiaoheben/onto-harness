"""Double-click launcher: prepare one local workspace and open its ready UI."""

import argparse
import json
import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

from ob_harness.common import HarnessError, safe_error
from ob_harness.services import Application
from ob_harness.services.application import initialize

ROOT = Path(__file__).resolve().parents[1]


def load_environment():
    path = ROOT / ".env"
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        name, separator, value = line.partition("=")
        name = name.strip()
        if separator and name in {"DEEPSEEK_API_KEY", "LOCAL_MODEL_API_KEY", "DEEPSEEK_MODEL"}:
            if not os.environ.get(name):
                os.environ[name] = value.strip().strip('"').strip("'")


def prepare(workspace: Path, ontology_id: str):
    if not (workspace / "harness.json").exists():
        if ontology_id != "demo":
            raise HarnessError(
                "invalid_config", "首次启动使用默认 demo，其他本体请指定已有工作区。"
            )
        if not os.environ.get("DEEPSEEK_API_KEY"):
            raise HarnessError(
                "missing_credentials", "请在 harness/.env 填写 DEEPSEEK_API_KEY 后重新双击启动。"
            )
        initialize(workspace, writable=True)
        path = workspace / "harness.json"
        config = json.loads(path.read_text(encoding="utf-8"))
        config["model"] = {
            "provider": "deepseek",
            "base_url": "https://api.deepseek.com",
            "model": os.environ.get("DEEPSEEK_MODEL", "deepseek-flash"),
            "api_key_env": "DEEPSEEK_API_KEY",
            "timeout": 60,
            "retries": 0,
            "max_output_tokens": 2048,
            "json_mode": True,
            "thinking": {"type": "disabled"},
        }
        path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    app = Application(workspace)
    app.ontologies.read(ontology_id)
    app.authorizer.require("propose", ontology_id)
    model = app.config["model"]
    credential = model.get(
        "api_key_env", "DEEPSEEK_API_KEY" if model["provider"] == "deepseek" else ""
    )
    if credential and not os.environ.get(credential):
        raise HarnessError(
            "missing_credentials", "模型密钥未设置；请检查 harness/.env 或当前进程环境。"
        )
    return app


def info_at(port):
    try:
        opener = build_opener(ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{port}/api/info", timeout=0.5) as response:
            return json.loads(response.read(65536))
    except (OSError, ValueError, URLError):
        return {}


def same_app(info, workspace, ontology_id):
    return (
        isinstance(info, dict)
        and isinstance(info.get("workspace"), str)
        and Path(info["workspace"]).resolve() == workspace.resolve()
        and info.get("ontology_id") == ontology_id
        and info.get("editable") is True
    )


def choose_port(start, workspace, ontology_id):
    for port in range(start, min(start + 10, 65536)):
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", port))
                return port, False
            except OSError:
                if same_app(info_at(port), workspace, ontology_id):
                    return port, True
    raise HarnessError("port_busy", "附近端口均被占用，请稍后再试或使用 --port 指定端口。")


def open_when_ready(port, workspace, ontology_id):
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        if same_app(info_at(port), workspace, ontology_id):
            webbrowser.open(f"http://127.0.0.1:{port}")
            return
        time.sleep(0.2)
    print("浏览器未自动打开，可手动访问上方地址。", flush=True)


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=ROOT / "workspace")
    parser.add_argument("--ontology", default="demo")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Prepare and check locally without starting a server or calling the model",
    )
    args = parser.parse_args(argv)
    try:
        if not 1 <= args.port <= 65535:
            raise HarnessError("invalid_config", "端口必须在 1–65535 之间。")
        load_environment()
        workspace = args.workspace.resolve()
        app = prepare(workspace, args.ontology)
        planner = "scripted" if app.config["model"]["provider"] == "replay" else "llm"
        if args.check:
            print(
                json.dumps(
                    {
                        "ready": True,
                        "workspace": str(workspace),
                        "ontology": args.ontology,
                        "provider": app.config["model"]["provider"],
                        "planner": planner,
                    },
                    ensure_ascii=False,
                )
            )
            return 0
        port, existing = choose_port(args.port, workspace, args.ontology)
        print(f"本体助手：http://127.0.0.1:{port}", flush=True)
        print("数据自动保存在工作区。关闭此服务窗口或按 Ctrl+C 停止服务。", flush=True)
        if existing:
            if not args.no_browser:
                webbrowser.open(f"http://127.0.0.1:{port}")
            print("已打开正在运行的本体助手。", flush=True)
            return 0
        from ob_harness.web.server import run_server

        if not args.no_browser:
            threading.Thread(
                target=open_when_ready, args=(port, workspace, args.ontology), daemon=True
            ).start()
        run_server(workspace, args.ontology, planner=planner, port=port, editable=True)
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as error:
        detail = safe_error(error)
        if isinstance(error, ImportError):
            detail["message"] = (
                '网页依赖缺失，请运行 .venv\\Scripts\\python.exe -m pip install -e ".[web]"。'
            )
        print("启动失败：" + detail["message"], file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
