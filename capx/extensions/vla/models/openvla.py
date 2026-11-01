"""First verified adapter. OpenVLA details do not leak into the execution loop."""
import hashlib
import time

from ..adapters import ActionChunk
from ..contracts import LIBERO_OSC

MODEL = "openvla/openvla-7b-finetuned-libero-spatial"
REVISION = "962318cec55ac10993ff0f5f43eda9a270b4c873"
CODE_REVISION = "47a0ec7fc4ec123775a391911046cf33cf9ed83f"
ACTION_CONTRACT = LIBERO_OSC


def preprocess(raw, tf, np, crop_area=.9):
    image = raw[::-1, ::-1].copy()
    image = tf.io.decode_image(tf.io.encode_jpeg(image), expand_animations=False, dtype=tf.uint8)
    image = tf.image.resize(image, (224, 224), method="lanczos3", antialias=True)
    image = tf.cast(tf.clip_by_value(tf.round(image), 0, 255), tf.uint8)
    image = tf.image.convert_image_dtype(image, tf.float32)
    offset = (1 - crop_area ** .5) / 2
    image = tf.image.crop_and_resize(image[None], [[offset, offset, 1-offset, 1-offset]], [0], (224,224))[0]
    return np.asarray(tf.image.convert_image_dtype(tf.clip_by_value(image, 0, 1), tf.uint8, saturate=True))


class OpenVlaAdapter:
    def __init__(self, model_id=MODEL, revision=REVISION, code_revision=CODE_REVISION,
                 unnorm_key="libero_spatial", seed=7):
        import numpy as np
        import tensorflow as tf
        tf.config.set_visible_devices([], "GPU")
        import torch
        from transformers import AutoModelForVision2Seq, AutoProcessor
        if not torch.cuda.is_available():
            raise RuntimeError("A CUDA GPU is required")
        torch.manual_seed(seed)
        np.random.seed(seed)
        tf.random.set_seed(seed)
        self.np, self.tf, self.torch = np, tf, torch
        self.unnorm_key = unnorm_key
        kwargs = dict(revision=revision, code_revision=code_revision, trust_remote_code=True)
        self.processor = AutoProcessor.from_pretrained(model_id, **kwargs)
        self.model = AutoModelForVision2Seq.from_pretrained(
            model_id, **kwargs, torch_dtype=torch.bfloat16, attn_implementation="sdpa",
            low_cpu_mem_usage=True).to("cuda").eval()
        if unnorm_key not in self.model.norm_stats:
            raise RuntimeError("Required normalization statistics missing")
        self.metadata = {"model": model_id, "revision": revision, "code_revision": code_revision,
                         "adapter": "openvla", "unnorm_key": unnorm_key,
                         "action_contract": ACTION_CONTRACT, "device": torch.cuda.get_device_name(),
                         "torch": torch.__version__, "center_crop_area": .9,
                         "attention": "sdpa", "quantized": False, "seed": seed,
                         "required_views": ["agentview"], "uses_robot_state": False}

    def predict(self, observation, instruction):
        from PIL import Image
        if observation.get("image_convention") != "libero_raw":
            raise ValueError("OpenVLA LIBERO preprocessing requires raw simulator images")
        np, tf, torch = self.np, self.tf, self.torch
        prepared = preprocess(observation["images"]["agentview"], tf, np)
        prompt = f"In: What action should the robot take to {instruction.lower()}?\nOut:"
        inputs = self.processor(prompt, Image.fromarray(prepared)).to("cuda", dtype=torch.bfloat16)
        started = time.monotonic()
        with torch.inference_mode():
            proposed = np.asarray(self.model.predict_action(
                **inputs, unnorm_key=self.unnorm_key, do_sample=False), dtype=np.float64)
        if proposed.shape != (7,) or not np.isfinite(proposed).all():
            raise ValueError("Expected seven finite policy outputs")
        native = proposed.copy()
        native[-1] = -np.sign(2 * proposed[-1] - 1)
        return ActionChunk([native.tolist()], ACTION_CONTRACT, {
            "proposed_action": proposed.tolist(), "inference_seconds": time.monotonic()-started,
            "policy_image_sha256": hashlib.sha256(prepared.tobytes()).hexdigest()})
