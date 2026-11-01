"""Lossless multiview observations and robot state over bounded JSON transport."""
import base64
import hashlib
import io
import json

import numpy as np
from PIL import Image


def digest(observation):
    return hashlib.sha256(json.dumps(observation, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def encode_observation(observation):
    images = {}
    for name, pixels in observation["images"].items():
        pixels = np.asarray(pixels)
        if pixels.dtype != np.uint8 or pixels.ndim != 3 or pixels.shape[2] != 3:
            raise ValueError("Policy images must be HWC RGB uint8")
        buffer = io.BytesIO()
        Image.fromarray(pixels).save(buffer, format="PNG")
        data = buffer.getvalue()
        images[name] = {"png_base64": base64.b64encode(data).decode(),
                        "sha256": hashlib.sha256(data).hexdigest()}
    result = {"images": images, "state": observation.get("state", {}),
              "image_convention": observation.get("image_convention", "unspecified")}
    digest(result)  # Reject non-finite state before making a request.
    return result


def decode_observation(observation, expected_digest):
    if digest(observation) != expected_digest:
        raise ValueError("Observation digest mismatch")
    images = {}
    for name, item in observation["images"].items():
        data = base64.b64decode(item["png_base64"], validate=True)
        if hashlib.sha256(data).hexdigest() != item["sha256"]:
            raise ValueError("Camera image digest mismatch")
        images[name] = np.asarray(Image.open(io.BytesIO(data)).convert("RGB"))
    return {**observation, "images": images}
