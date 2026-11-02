"""Finite, model-independent reverse inference worker."""
from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from .adapters import load_adapter
from .codec import decode_observation


def run(connection_file, output, seconds=600, adapter="openvla", adapter_config=None):
    connection = json.loads(Path(connection_file).read_text())
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    worker_id = uuid.uuid4().hex
    registered = False

    def request(path, body=None):
        req = urllib.request.Request(connection["url"].rstrip("/") + path,
            data=None if body is None else json.dumps(body, allow_nan=False).encode(),
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + connection["token"]})
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.load(response)

    try:
        model = load_adapter(adapter, adapter_config)
        metadata = dict(model.metadata, protocol_version=1, worker_id=worker_id)
        (output / "model.json").write_text(json.dumps(metadata, indent=2))
        request("/ready", metadata)
        registered = True
        print(json.dumps({"event": "policy_ready", "adapter": adapter,
                          "device": metadata.get("device")}), flush=True)
        end = time.monotonic() + seconds
        records = []
        while time.monotonic() < end:
            item = request("/next")
            if item["type"] == "close":
                break
            if item["type"] == "wait":
                continue
            if item.get("protocol_version") != 1:
                raise ValueError("Unsupported VLA protocol version")
            observation = decode_observation(item["observation"], item["observation_sha256"])
            chunk = model.predict(observation, item["instruction"])
            if chunk.action_contract != metadata["action_contract"]:
                raise ValueError("Adapter changed its declared action contract")
            reply = {key: item[key] for key in ("request_id", "context", "observation_sha256")}
            reply.update(native_actions=chunk.actions, action_contract=chunk.action_contract,
                         diagnostics=chunk.diagnostics, worker_id=worker_id)
            try:
                request("/result", reply)
            except urllib.error.HTTPError as error:
                if error.code != 409:
                    raise
                reply["discarded"] = True
            records.append(reply)
            (output / "predictions.json").write_text(json.dumps(records, indent=2))
        (output / "result.json").write_text(json.dumps({"predictions": len(records)}))
    except BaseException as error:
        try:
            request("/error", {"error": str(error), 'worker_id': worker_id if registered else None})
        except Exception:
            pass
        raise
    finally:
        try:
            request('/detach', {'worker_id': worker_id if registered else None})
        except Exception:
            pass  # The owner may have closed the bridge first.


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--connection", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--seconds", type=int, default=600)
    parser.add_argument("--adapter", default="openvla")
    parser.add_argument("--adapter-config", type=Path)
    args = parser.parse_args()
    config = json.loads(args.adapter_config.read_text()) if args.adapter_config else None
    run(args.connection, args.output, args.seconds, args.adapter, config)
