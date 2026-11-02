"""Explicit decoding of the LIBERO-trained OpenVLA controller convention."""
import numpy as np

from ..adapters import ActionChunk
from ..contracts import LIBERO_OSC, WORLD_DELTA


def orient_image(pixels, convention):
    image = np.asarray(pixels)
    if image.ndim != 3 or image.shape[-1] != 3 or image.dtype != np.uint8:
        raise ValueError('Expected an RGB uint8 image')
    if convention == 'libero_raw':
        return image[::-1, ::-1].copy()
    if convention == 'rgb_top_left':
        return image.copy()
    raise ValueError('Unsupported image convention')


class OpenVlaActionDecoder:
    def __init__(self, output_contract=LIBERO_OSC, osc_action_scale=None, motion_gain=1.):
        if output_contract not in {LIBERO_OSC, WORLD_DELTA}:
            raise ValueError('Unsupported OpenVLA output contract')
        if not np.isfinite(motion_gain) or not 0 < motion_gain <= 1:
            raise ValueError('motion_gain must be in (0,1]')
        self.output_contract, self.motion_gain = output_contract, float(motion_gain)
        self.scale = None
        if output_contract == WORLD_DELTA:
            self.scale = np.asarray(osc_action_scale, dtype=float)
            if self.scale.shape != (6,) or not np.isfinite(self.scale).all() or np.any(self.scale <= 0):
                raise ValueError('WORLD delta requires six explicit positive OSC scales (meters/radians)')
        elif osc_action_scale is not None or motion_gain != 1:
            raise ValueError('Native LIBERO output must not change scale or gain')
        self.metadata = dict(action_contract=output_contract, model_output_contract=LIBERO_OSC,
            action_decoder='openvla_libero_osc_to_world_delta.v1' if self.scale is not None else 'openvla_libero_native.v1',
            osc_action_scale=self.scale.tolist() if self.scale is not None else None,
            motion_gain=self.motion_gain, gripper_decoding='-sign(2*model_gripper-1)')

    def decode(self, proposed):
        proposed = np.asarray(proposed, dtype=float)
        if (proposed.shape != (7,) or not np.isfinite(proposed).all()
                or np.any(np.abs(proposed[:6]) > 1) or not 0 <= proposed[-1] <= 1):
            raise ValueError('Expected bounded LIBERO-normalized XYZ/rotation and gripper in [0,1]')
        decoded = proposed.copy()
        decoded[-1] = -np.sign(2*proposed[-1]-1)
        if self.scale is not None:
            decoded[:6] *= self.scale*self.motion_gain
        return ActionChunk([decoded.tolist()], self.output_contract, dict(proposed_action=proposed.tolist()))
