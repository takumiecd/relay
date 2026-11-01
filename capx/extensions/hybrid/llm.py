"""Finite OpenAI-compatible code generation for a user-selected GPU/API endpoint."""

from __future__ import annotations

import ast
import base64
import io
import json
import os
import re
import urllib.request
from urllib.parse import urlsplit

from PIL import Image


def extract_code(content):
    if not isinstance(content, str) or not content.strip() or len(content) > 32_000:
        raise ValueError("Expected a finite Python response")
    blocks = re.findall(r"```(?:python|py)?\s*\n(.*?)```", content, flags=re.S)
    if len(blocks) > 1:
        raise ValueError("Return one complete Python program")
    code = (blocks[0] if blocks else content).strip()
    ast.parse(code)
    return code


class HttpCodeGenerator:
    def __init__(self, url, model, *, images=False, key_env="CAPX_LLM_API_KEY", timeout=45):
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username
            or parsed.password
            or parsed.query
        ):
            raise ValueError("Use an HTTP(S) endpoint without credentials or query parameters")
        self.url, self.model, self.images, self.timeout = url, model, images, timeout
        self.key_env = key_env
        self.metadata = dict(generator="llm_http", model=model, endpoint=url, images=images)

    def generate(self, observation, instruction, api_docs):
        content = [
            {
                "type": "text",
                "text": instruction + "\nRobot state:\n" + json.dumps(observation["state"]),
            }
        ]
        if self.images:
            for name, pixels in observation["images"].items():
                # Convert raw simulator orientation to an operator-view image for the VLM.
                image = (
                    pixels[::-1, ::-1].copy()
                    if observation["image_convention"] == "libero_raw"
                    else pixels
                )
                buffer = io.BytesIO()
                Image.fromarray(image).save(buffer, format="PNG")
                content.extend(
                    [
                        {"type": "text", "text": f"Camera: {name}"},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": "data:image/png;base64,"
                                + base64.b64encode(buffer.getvalue()).decode()
                            },
                        },
                    ]
                )
        messages = [
            {
                "role": "system",
                "content": "Return one finite Python program, no explanation. Use the provided tools only. "
                "WORLD XYZ meters; quaternion WXYZ. Use short local motions. "
                "At most 100 executed actions total; do not import, reset, launch processes or access files. "
                "run_vla delegates a bounded primitive and then returns to your code.\n" + api_docs,
            },
            {"role": "user", "content": content},
        ]
        headers = {"Content-Type": "application/json"}
        key = os.environ.get(self.key_env)
        if key:
            headers["Authorization"] = "Bearer " + key
        request = urllib.request.Request(
            self.url,
            headers=headers,
            data=json.dumps(
                {"model": self.model, "messages": messages, "temperature": 0.0, "max_tokens": 2048}
            ).encode(),
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            data = response.read(1_000_001)
        if len(data) > 1_000_000:
            raise ValueError("Code response exceeds limit")
        text = json.loads(data)["choices"][0]["message"]["content"]
        return {
            "code": extract_code(text),
            "metadata": self.metadata,
            "messages": messages,
            "raw_response": text,
        }
