"""Operate an existing Relay session from a terminal."""

from __future__ import annotations

import argparse
import json
import sys

from .client import RelayClientError, client_from_environment


def parser():
    result = argparse.ArgumentParser(prog="relay", description=__doc__)
    result.add_argument(
        "--url", help="Relay Console URL (default: RELAY_URL or http://127.0.0.1:8876)"
    )
    result.add_argument(
        "--timeout", type=float, default=5, help="HTTP request timeout in seconds (default: 5)"
    )
    result.add_argument(
        "--wait-timeout",
        type=float,
        default=30,
        help="Accepted operation completion timeout (default: 30)",
    )
    result.add_argument("--json", action="store_true", help="Print JSON for scripts")
    commands = result.add_subparsers(dest="command", required=True)
    leaves = [commands.add_parser("status", help="状態・接続・現在の経路を確認")]
    control = commands.add_parser("control", help="操作主体を切り替える")
    control.add_argument("mode", choices=["human", "vla", "llm", "stopped"])
    control.add_argument("--instruction", help="VLA／LLMへ渡す操作指示。LLMでは必須")
    leaves += [control, commands.add_parser("stop", help="Escと同じ停止要求を送る")]
    history = commands.add_parser("history", help="経路の一覧・巻き戻し・採用")
    operations = history.add_subparsers(dest="operation", required=True)
    leaves.append(operations.add_parser("list", help="経路とstep範囲を表示"))
    seek = operations.add_parser("seek", help="指定stepへ復元し停止。新しい分岐は次の操作で作成")
    seek.add_argument("--branch", help="経路ID。省略時は現在の経路")
    seek.add_argument("--step", type=int, required=True)
    adopt = operations.add_parser("adopt", help="経路を学習候補として採用。処理完了まで待つ")
    adopt.add_argument("--branch", help="経路ID。省略時は現在の経路")
    adopt.add_argument("--step", type=int, help="採用する末尾step。省略時は現在のcursor")
    leaves += [seek, adopt]
    for leaf in leaves:
        leaf.add_argument(
            "--json", action="store_true", default=argparse.SUPPRESS, help="Print JSON for scripts"
        )
    return result


def format_status(state):
    lines = [
        f"mode: {state['mode']}   epoch: {state['epoch']}",
        f"backend: {state.get('backend_id') or 'unknown'}",
        f"VLA: {'ready' if state.get('vla_ready') else 'unavailable'}   "
        f"LLM: {'ready' if state.get('llm_ready') else 'unavailable'}",
        f"recorded commands: {state.get('applied_steps', 0)}",
    ]
    timeline = state.get("timeline")
    if timeline:
        lines.append(
            f"history: {timeline['branch']}  step {timeline['cursor']} / {timeline['head']}"
        )
        adopted = timeline.get("adopted")
        lines.append(
            f"adopted: {adopted['branch']} through step {adopted['end_step']}"
            if adopted
            else "adopted: none"
        )
    else:
        lines.append("history: unsupported")
    pending = [key for key in ("resetting", "seeking", "adopting") if state.get(key)]
    if pending:
        lines.append("pending: " + ", ".join(pending))
    if state.get("error"):
        lines.append("error: " + str(state["error"]))
    elif state.get("message"):
        lines.append("message: " + state["message"])
    return "\n".join(lines)


def format_result(command, data):
    if command == "status":
        return format_status(data)
    if command == "list":
        lines = ["BRANCH  HEAD  PARENT  FORK_STEP"]
        for branch in data["branches"]:
            marker = "*" if branch["id"] == data["branch"] else " "
            lines.append(
                f"{marker}{branch['id']}  {branch['head']}  {branch['parent'] or '-'}  "
                f"{branch['parent_step'] if branch['parent_step'] is not None else '-'}"
            )
        return "\n".join(lines)
    response = data["response"]
    lines = [f"{command}: accepted  epoch: {response['epoch']}"]
    if command == "stop":
        lines.append("停止要求を受理しました")
        if data.get("state_confirmation") != "confirmed":
            lines.append("状態表示の更新は未確認です。statusで確認してください")
    elif "state" in data:
        lines.append(format_status(data["state"]))
        if command == "adopt":
            selected = data["state"]["timeline"]["adopted"]
            lines.append("route: " + selected["route"])
            lines.append("training_admitted: false")
    return "\n".join(lines)


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        client = client_from_environment(
            args.url, timeout=args.timeout, wait_timeout=args.wait_timeout
        )
        name = args.command
        if name == "status":
            result = client.status()
        elif name == "stop":
            result = client.stop()
        elif name == "control":
            result = client.control(args.mode, instruction=args.instruction)
        else:
            name = args.operation
            if name == "list":
                result = client.history()
            elif name == "seek":
                result = client.seek(args.step, branch=args.branch)
            else:
                result = client.adopt(step=args.step, branch=args.branch)
        print(
            json.dumps(result, ensure_ascii=False, indent=2)
            if args.json
            else format_result(name, result)
        )
        return 0
    except RelayClientError as error:
        if args.json:
            result = {"error": {"code": error.code, "message": str(error)}}
            if error.receipt is not None:
                result["receipt"] = error.receipt
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(f"relay: {error}", file=sys.stderr)
        return error.exit_code
    except KeyboardInterrupt:
        print(
            "relay: 待機を中断しました。受理済みの要求は取り消していません。status／stopで確認してください",
            file=sys.stderr,
        )
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
