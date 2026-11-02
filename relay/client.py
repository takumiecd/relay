"""Standard-library client for the existing loopback Relay Console API.

No simulator, CaP-X or ML imports. One client sends commands sequentially; a
fresh CLI invocation uses a fresh identity. Mutations are never retried.
"""

from __future__ import annotations

import json
import math
import os
import time
import uuid
from html.parser import HTMLParser
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class RelayClientError(RuntimeError):
    code = "request_failed"
    exit_code = 1

    def __init__(self, message, *, receipt=None):
        super().__init__(message)
        self.receipt = receipt


class RelayConflictError(RelayClientError):
    code = "conflict"
    exit_code = 3


class RelayPendingError(RelayClientError):
    code = "pending"
    exit_code = 4


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _TokenParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tokens = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "meta" and values.get("name") == "capx-token":
            self.tokens.append(values.get("content"))


class RelayClient:
    """Attach to a running Console; all execution stays on its owner thread."""

    def __init__(self, url="http://127.0.0.1:8876", *, token=None, timeout=5, wait_timeout=30):
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except (TypeError, ValueError) as error:
            raise RelayClientError("接続URLが不正です") from error
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "localhost"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or port is None
            or not 1 <= port <= 65535
        ):
            raise RelayClientError(
                "http://127.0.0.1:PORT または http://localhost:PORT を指定してください"
            )
        for value in (timeout, wait_timeout):
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 600:
                raise RelayClientError("timeoutは0より大きく600秒以下にしてください")
        if token is not None and not self._valid_token(token):
            raise RelayClientError("認証tokenが不正です")
        self.url = f"http://{parsed.hostname}:{port}"
        self.timeout, self.wait_timeout = float(timeout), float(wait_timeout)
        self.client_id = "relay-cli-" + uuid.uuid4().hex
        self._sequence = 0
        self._token = token
        # This API is local. Do not forward its token through an environment
        # proxy or follow a redirect to a different service.
        self._opener = build_opener(ProxyHandler({}), _NoRedirect())

    @staticmethod
    def _valid_token(token):
        return (
            isinstance(token, str)
            and 0 < len(token) <= 512
            and all(33 <= ord(char) <= 126 for char in token)
        )

    def _http(self, path, data=None, *, authenticated=True, timeout=None):
        if authenticated and self._token is None:
            parser = _TokenParser()
            try:
                parser.feed(self._http("/", authenticated=False, timeout=timeout).decode("utf-8"))
            except UnicodeDecodeError:
                raise RelayClientError("操作画面の応答形式が不正です") from None
            if len(parser.tokens) != 1 or not self._valid_token(parser.tokens[0]):
                raise RelayClientError(
                    "Relayの操作画面から認証情報を取得できません。接続先を確認してください"
                )
            self._token = parser.tokens[0]
        headers = {"Accept": "application/json" if authenticated else "text/html"}
        if authenticated:
            headers["X-CapX-Token"] = self._token
        payload = None
        if data is not None:
            headers["Content-Type"] = "application/json"
            payload = json.dumps(data, allow_nan=False).encode()
        request = Request(self.url + path, data=payload, headers=headers)
        try:
            with self._opener.open(request, timeout=timeout or self.timeout) as response:
                raw = response.read(4_000_001)
        except HTTPError as error:
            if error.code == 403:
                message = (
                    "接続を拒否されました。URLを確認し、サーバー再起動後なら接続し直してください"
                )
            else:
                try:
                    message = json.loads(error.read(40001)).get("error", f"HTTP {error.code}")
                except (ValueError, AttributeError, OSError, HTTPException):
                    message = f"HTTP {error.code}"
            raise RelayClientError(str(message)[:500]) from None
        except (URLError, TimeoutError, OSError, HTTPException):
            message = "Relayに接続できないか、応答が期限内に届きませんでした"
            if data is not None:
                message += "。要求は受理済みの可能性があります。自動再送はしません。statusで確認してください"
            raise RelayClientError(message) from None
        if len(raw) > 4_000_000:
            raise RelayClientError("応答が大きすぎます")
        if not authenticated:
            return raw
        try:
            result = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise RelayClientError("APIの応答がJSONではありません") from None
        if not isinstance(result, dict):
            raise RelayClientError("APIの応答形式が不正です")
        return result

    def status(self, *, _timeout=None):
        deadline = time.monotonic() + (_timeout or self.timeout)
        while True:
            result = self._http("/api/state", timeout=max(0.001, deadline - time.monotonic()))
            context = result.get("context", {})
            if (
                not isinstance(result.get("mode"), str)
                or result["mode"] not in {"STOPPED", "HUMAN", "VLA", "LLM"}
                or type(result.get("epoch")) is not int
                or not isinstance(context, dict)
                or not isinstance(context.get("session_id"), str)
                or not context["session_id"]
                or type(context.get("epoch")) is not int
            ):
                raise RelayClientError("Relayの状態応答が不正です")
            if context["epoch"] == result["epoch"]:
                return result
            # Older Consoles may publish a snapshot while an HTTP caller is
            # changing authority. Re-read only this GET, never a mutation.
            if time.monotonic() >= deadline:
                raise RelayClientError(
                    "状態更新中のため一貫したepochを取得できません。statusを確認してください"
                )
            time.sleep(min(0.05, max(0, deadline - time.monotonic())))

    def _post(self, action, path, fields, epoch):
        sequence, self._sequence = self._sequence, self._sequence + 1
        response = self._http(
            path, dict(fields, client_id=self.client_id, sequence=sequence, expected_epoch=epoch)
        )
        receipt = {
            "action": action,
            "accepted": response.get("accepted") is True,
            "completed": False,
            "client_id": self.client_id,
            "sequence": sequence,
            "response": response,
        }
        if not receipt["accepted"]:
            raise RelayConflictError(
                "操作状態が変わったため要求は拒否されました。statusを確認してください",
                receipt=receipt,
            )
        if type(response.get("epoch")) is not int:
            raise RelayClientError("要求の受理epochを確認できません", receipt=receipt)
        return receipt

    @staticmethod
    def _available(state):
        if any(state.get(k) for k in ("resetting", "seeking", "adopting")):
            raise RelayClientError("履歴または試行の処理中です。statusで完了を確認してください")

    def _wait(self, receipt, before, predicate):
        deadline = time.monotonic() + self.wait_timeout
        while time.monotonic() < deadline:
            try:
                last = self.status(
                    _timeout=min(self.timeout, max(0.001, deadline - time.monotonic()))
                )
            except RelayClientError as error:
                error.receipt = receipt
                raise
            receipt["state"] = last
            if last["context"]["session_id"] != before["context"]["session_id"]:
                raise RelayConflictError(
                    "接続先セッションが変わりました。自動再送はしません", receipt=receipt
                )
            if last["epoch"] >= receipt["response"]["epoch"] and predicate(last):
                receipt["completed"] = True
                return receipt
            time.sleep(min(0.05, max(0, deadline - time.monotonic())))
        raise RelayPendingError(
            "要求は受理されましたが、完了待ちが期限を超えました。statusで確認してください",
            receipt=receipt,
        )

    def stop(self):
        # STOP is accepted with a stale epoch. It must not depend on a state
        # GET completing first; this is the same authority invalidation as Esc.
        receipt = self._post("stop", "/api/mode", {"mode": "STOPPED"}, 0)
        receipt["completed"] = True
        # Let a subsequent CLI invocation see the accepted epoch instead of a
        # pre-stop cached snapshot. Failure of this read does not erase the
        # accepted STOP, and a stuck owner thread cannot block this indefinitely.
        deadline = time.monotonic() + min(1.0, self.wait_timeout)
        while time.monotonic() < deadline:
            try:
                state = self.status(_timeout=max(0.001, deadline - time.monotonic()))
            except RelayClientError:
                receipt["state_confirmation"] = "unavailable"
                return receipt
            if state["epoch"] >= receipt["response"]["epoch"]:
                receipt["state"] = state
                if state["mode"] != "STOPPED":
                    receipt["state_confirmation"] = "superseded"
                    raise RelayConflictError(
                        "停止要求の後に別の操作が開始されました。現在の状態を確認してください",
                        receipt=receipt,
                    )
                receipt["state_confirmation"] = "confirmed"
                return receipt
            time.sleep(min(0.05, max(0, deadline - time.monotonic())))
        receipt["state_confirmation"] = "pending"
        return receipt

    def control(self, mode, *, instruction=None):
        if not isinstance(mode, str):
            raise RelayClientError("操作主体はhuman、vla、llmのいずれかです")
        mode = mode.upper()
        if mode == "STOPPED":
            return self.stop()
        if mode not in {"HUMAN", "VLA", "LLM"}:
            raise RelayClientError("操作主体はhuman、vla、llmのいずれかです")
        if instruction is not None and (
            not isinstance(instruction, str)
            or not instruction.strip()
            or len(instruction) > 2000
            or mode == "HUMAN"
        ):
            raise RelayClientError("instructionはVLA／LLM向けの1〜2000文字の指示にしてください")
        if mode == "LLM" and instruction is None:
            raise RelayClientError("LLMには --instruction で操作指示を指定してください")
        state = self.status()
        self._available(state)
        if state.get("ended"):
            raise RelayClientError(
                "この試行は終了しています。履歴へ戻るか新しい試行を始めてください"
            )
        if mode in {"VLA", "LLM"} and not state.get(mode.lower() + "_ready"):
            raise RelayClientError(f"{mode}が準備できていません。接続先を確認してください")
        fields = {"mode": mode}
        if instruction is not None:
            fields["instruction"] = instruction.strip()
        receipt = self._post("control", "/api/mode", fields, state["epoch"])
        return self._wait(receipt, state, lambda current: True)

    def history(self):
        state = self.status()
        if not state.get("timeline"):
            raise RelayClientError("この接続先は巻き戻し・経路採用に対応していません")
        return state["timeline"]

    def _history_command(self, action, branch, step):
        state = self.status()
        self._available(state)
        timeline = state.get("timeline")
        if not timeline:
            raise RelayClientError("この接続先は巻き戻し・経路採用に対応していません")
        branch = branch if branch is not None else timeline["branch"]
        step = step if step is not None else timeline["cursor"]
        known = {b["id"]: b["head"] for b in timeline["branches"]}
        if (
            not isinstance(branch, str)
            or branch not in known
            or type(step) is not int
            or not 0 <= step <= known[branch]
        ):
            raise RelayClientError("記録されたbranchとstepの範囲内を指定してください")
        if action == "adopt" and step == 0:
            raise RelayClientError("採用には操作を含むstepを指定してください")
        field = "step" if action == "seek" else "end_step"
        receipt = self._post(
            action, "/api/" + action, {"branch": branch, field: step}, state["epoch"]
        )
        previous_id = (timeline.get("adopted") or {}).get("id")

        def complete(current):
            if current.get("seeking") or current.get("adopting") or current.get("resetting"):
                return False
            if current.get("error"):
                raise RelayClientError(str(current["error"])[:500], receipt=receipt)
            history = current.get("timeline") or {}
            if action == "seek":
                matches = history.get("branch") == branch and history.get("cursor") == step
            else:
                adopted = history.get("adopted") or {}
                matches = (
                    adopted.get("branch") == branch
                    and adopted.get("end_step") == step
                    and adopted.get("selected_by") == self.client_id
                    and adopted.get("id") != previous_id
                )
            if not matches:
                raise RelayConflictError(
                    "指定した履歴処理の完了を確認できません。別の操作または実行エラーを確認してください",
                    receipt=receipt,
                )
            return True

        return self._wait(receipt, state, complete)

    def seek(self, step, *, branch=None):
        return self._history_command("seek", branch, step)

    def adopt(self, *, branch=None, step=None):
        return self._history_command("adopt", branch, step)


def client_from_environment(url=None, **kwargs):
    """The page supplies its token by default; RELAY_TOKEN can override it."""
    return RelayClient(
        url or os.environ.get("RELAY_URL", "http://127.0.0.1:8876"),
        token=os.environ.get("RELAY_TOKEN"),
        **kwargs,
    )
