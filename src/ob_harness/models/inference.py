import asyncio
import json
import os
import socket
import time
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from ..common import HarnessError, canonical, digest, positive_int
from ..domain import LLMRequest, LLMResponse

AttemptHook = Callable[[], None]


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def validate_settings(settings: dict[str, Any]) -> None:
    forbidden = {"api_key", "authorization", "token", "password", "headers"}
    if forbidden & {key.casefold() for key in settings}:
        raise HarnessError(
            "unsafe_config", "Use api_key_env; inline credentials and headers are not accepted"
        )
    for value in settings.values():
        if isinstance(value, dict):
            validate_secret_fields(value, forbidden)
    if settings.get("provider", "replay") not in {"replay", "api", "local", "deepseek"}:
        raise HarnessError("invalid_model", "Supported providers: replay, api, local, deepseek")
    if settings.get("provider", "replay") != "replay":
        if not isinstance(settings.get("model"), str) or not settings["model"].strip():
            raise HarnessError("invalid_model", "Configure an explicit model name")
        url = urlparse(settings.get("base_url", "https://api.deepseek.com"))
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise HarnessError(
                "invalid_model", "base_url must be a credential-free HTTP(S) endpoint"
            )
        timeout = settings.get("timeout", 60)
        if type(timeout) not in (int, float) or not 0 < timeout <= 600:
            raise HarnessError("invalid_model", "timeout must be between 0 and 600 seconds")
        retries = settings.get("retries", 1)
        if type(retries) is not int or not 0 <= retries <= 3:
            raise HarnessError("invalid_model", "retries must be an integer between 0 and 3")
        positive_int(settings.get("max_output_tokens", 2048), "max_output_tokens", maximum=131072)
        if not isinstance(settings.get("api_key_env", ""), str):
            raise HarnessError("invalid_model", "api_key_env must be a variable name")


def validate_secret_fields(value: dict[str, Any], forbidden: set[str]) -> None:
    if forbidden & {str(key).casefold() for key in value}:
        raise HarnessError("unsafe_config", "Inline model credentials are not accepted")
    for child in value.values():
        if isinstance(child, dict):
            validate_secret_fields(child, forbidden)


class ChatModel:
    def __init__(self, settings: dict[str, Any]):
        validate_settings(settings)
        self.settings = dict(settings)
        self.timeout = float(settings.get("timeout", 60))
        base = settings.get("base_url", "https://api.deepseek.com").rstrip("/")
        self.url = base if base.endswith("/chat/completions") else base + "/chat/completions"

    def generate(
        self, request: LLMRequest, *, on_attempt: AttemptHook | None = None
    ) -> LLMResponse:
        env_name = self.settings.get(
            "api_key_env", "DEEPSEEK_API_KEY" if self.settings.get("provider") == "deepseek" else ""
        )
        key = os.environ.get(env_name, "") if env_name else ""
        if env_name and not key:
            raise HarnessError(
                "missing_credentials", "Configured model credential environment variable is empty"
            )
        body: dict[str, Any] = {
            "model": self.settings["model"],
            "messages": request.messages,
            "stream": False,
            "max_tokens": self.settings.get("max_output_tokens", 2048),
            "temperature": self.settings.get(
                "temperature", self.settings.get("parameters", {}).get("temperature", 0)
            ),
        }
        if self.settings.get("json_mode", True):
            body["response_format"] = {"type": "json_object"}
        for name in ("thinking", "reasoning_effort"):
            if name in self.settings:
                body[name] = self.settings[name]
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if key:
            headers["Authorization"] = "Bearer " + key
        opener = build_opener(NoRedirect())
        started, attempts = time.monotonic(), 0
        for attempt in range(self.settings.get("retries", 1) + 1):
            if on_attempt:
                on_attempt()
            attempts += 1
            try:
                operation = Request(
                    self.url, canonical(body).encode("utf-8"), headers, method="POST"
                )
                with opener.open(operation, timeout=self.timeout) as stream:
                    data = stream.read(8 * 1024 * 1024 + 1)
                if len(data) > 8 * 1024 * 1024:
                    raise HarnessError(
                        "model_response_too_large", "Model response exceeded the transport limit"
                    )
                response = json.loads(data)
                choice = response["choices"][0]
                text = choice["message"].get("content")
                if choice.get("finish_reason") == "length":
                    raise HarnessError(
                        "model_output_truncated",
                        "Model output was truncated; increase output budget or reduce input",
                    )
                if not isinstance(text, str) or not text.strip():
                    raise HarnessError("empty_model_response", "Model returned no text content")
                usage = response.get("usage") or {}
                return LLMResponse(
                    text,
                    response.get("model", self.settings["model"]),
                    {
                        "input_tokens": usage.get("prompt_tokens"),
                        "output_tokens": usage.get("completion_tokens"),
                        "total_tokens": usage.get("total_tokens"),
                    },
                    {
                        "provider": self.settings["provider"],
                        "attempts": attempts,
                        "elapsed_seconds": round(time.monotonic() - started, 3),
                        "finish_reason": choice.get("finish_reason"),
                        "replay": False,
                    },
                )
            except HTTPError as error:
                error.close()
                retryable = error.code in {408, 429, 500, 502, 503, 504}
                if retryable and attempt < self.settings.get("retries", 1):
                    time.sleep(min(2**attempt, 4))
                    continue
                code = "model_authentication" if error.code in (401, 403) else "model_http_error"
                raise HarnessError(
                    code, f"Model endpoint returned HTTP {error.code}", retryable=retryable
                ) from None
            except (URLError, TimeoutError, socket.timeout):
                if attempt < self.settings.get("retries", 1):
                    time.sleep(min(2**attempt, 4))
                    continue
                raise HarnessError(
                    "model_transport_error",
                    "Model request timed out or could not connect",
                    retryable=True,
                ) from None
            except (ValueError, KeyError, IndexError, TypeError) as error:
                if isinstance(error, HarnessError):
                    raise
                raise HarnessError(
                    "model_protocol_error", "Model endpoint returned an incompatible response"
                ) from None
        raise HarnessError("model_failed", "Model request failed")

    async def agenerate(
        self, request: LLMRequest, *, on_attempt: AttemptHook | None = None
    ) -> LLMResponse:
        # Meter on the event-loop thread. A cancelled socket thread must not later
        # mutate a saved run or start another retry in the background.
        single = ChatModel({**self.settings, "retries": 0})
        started = time.monotonic()
        for attempt in range(self.settings.get("retries", 1) + 1):
            if on_attempt:
                on_attempt()
            try:
                result = await asyncio.to_thread(single.generate, request)
                result.metadata.update(
                    attempts=attempt + 1, elapsed_seconds=round(time.monotonic() - started, 3)
                )
                return result
            except HarnessError as error:
                if not error.retryable or attempt >= self.settings.get("retries", 1):
                    raise
                await asyncio.sleep(min(2**attempt, 4))
        raise HarnessError("model_failed", "Model request failed")


class ReplayModel:
    def __init__(self, settings: dict[str, Any]):
        validate_settings(settings)
        self.settings = dict(settings)
        responses = settings.get("responses")
        if responses is None and settings.get("responses_path"):
            responses = json.loads(Path(settings["responses_path"]).read_text(encoding="utf-8-sig"))
        if not isinstance(responses, list):
            raise HarnessError("invalid_replay", "Replay requires a response fixture list")
        self.responses = responses
        self.fixture_hash = digest(responses)

    def generate(
        self, request: LLMRequest, *, on_attempt: AttemptHook | None = None
    ) -> LLMResponse:
        if on_attempt:
            on_attempt()
        matches = []
        for item in self.responses:
            match = item.get("match", {})
            if "request_hash" in match:
                matched = match["request_hash"] == digest(request.messages)
            elif set(match) == {"task", "input_hash"}:
                matched = all(request.metadata.get(key) == value for key, value in match.items())
            else:
                raise HarnessError(
                    "invalid_replay", "Fixture needs request_hash or exact task+input_hash matching"
                )
            if matched:
                matches.append(item)
        if len(matches) != 1:
            raise HarnessError("replay_mismatch", "Replay input must match exactly one fixture")
        text = matches[0]["response"]
        text = canonical(text) if isinstance(text, dict) else text
        if not isinstance(text, str):
            raise HarnessError("invalid_replay", "Replay response must be a string or object")
        return LLMResponse(
            text,
            "replay-fixture",
            {"input_tokens": None, "output_tokens": None, "total_tokens": None},
            {
                "replay": True,
                "fixture_hash": self.fixture_hash,
                "match": matches[0]["match"],
                "attempts": 1,
            },
        )

    async def agenerate(
        self, request: LLMRequest, *, on_attempt: AttemptHook | None = None
    ) -> LLMResponse:
        return self.generate(request, on_attempt=on_attempt)


def build_model(settings: dict[str, Any]) -> ChatModel | ReplayModel:
    validate_settings(settings)
    return (
        ReplayModel(settings)
        if settings.get("provider", "replay") == "replay"
        else ChatModel(settings)
    )
