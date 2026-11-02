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
            if observation.get('image_convention') != 'rgb_top_left':
                raise ValueError('Use backend.code_observation() with rgb_top_left images')
            for name, pixels in observation["images"].items():
                buffer = io.BytesIO()
                Image.fromarray(pixels).save(buffer, format="PNG")
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
                "Use short local motions in the units and order declared by the available tools. "
                "At most 100 executed actions total; do not import, reset, launch processes or access files. "
                "Call only functions listed below; a VLA may be unavailable. "
                "RESULT may contain a final return value.\n" + api_docs +
                ("\nWORLD XYZ meters; quaternion WXYZ. Read get_robot_state() before motion; "
                 "robot_cartesian_pos is [x,y,z,qw,qx,qy,qz]. move_to_pose position_m takes "
                 "EXACTLY THREE absolute coordinates, never a 7-element pose or a relative displacement.\n"
                 "Example: move up by 1cm relative to NOW (not an absolute z of 0.01):\n"
                 "s = get_robot_state()\n"
                 "p = s['robot_cartesian_pos'][:3]\n"
                 "RESULT = move_to_pose([p[0], p[1], p[2] + 0.01], max_actions=30)\n"
                 "For -X instead, use [p[0] - distance_m, p[1], p[2]]. "
                 "For a subsequent motion, get_robot_state() again. "
                 "Omit quaternion_wxyz to preserve orientation.\n"
                 if 'move_to_pose(' in api_docs else '') +
                ("\nJoint positions are absolute radians, ordered by arm_joint_names from get_robot_state(). "
                 "Read current robot_joint_pos before moving. Example: move joint 1 by +0.04rad:\n"
                 "s = get_robot_state()\nq = s['robot_joint_pos'][:]\nq[0] += 0.04\n"
                 "RESULT = move_joints(q, max_actions=10)\n"
                 if 'move_joints(' in api_docs else ''),
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
