"""Manual translation frames; independent of policy/control action contracts."""
import numpy as np

from relay.backends.base import PreflightRejectedError


LIBERO_REFERENCE_FRAMES = [
    dict(id='world', label='ワールド', description='W/S ±X、A/D ±Y、R/F 上下'),
    dict(id='agentview', label='全体カメラ',
         description='W/S 机の奥・手前、A/D 画面の左右、R/F 高さの上下'),
    dict(id='wrist', label='手首カメラ',
         description='W/S 画面の上下、A/D 画面の左右、R/F 高さの上下'),
]


def camera_translation_axes(axes, right, up, *, planar=False):
    """Map W/S, A/D to displayed right/up, retaining WORLD Z and rotations.

    axes[0] is W minus S; axes[1] is A minus D. The adapter supplies axes
    AFTER its display-image flips. Fixed overview motion stays on the table;
    wrist motion follows its current image plane. A degenerate table projection
    is refused before execution. Bound mixed/diagonal translations uniformly
    to retain their direction within the native normalized input range.
    """
    axes = np.asarray(axes, dtype=float)
    right, up = np.array(right, dtype=float), np.array(up, dtype=float)
    if (axes.shape != (6,) or not np.isfinite(axes).all() or (np.abs(axes) > 1).any()
            or right.shape != (3,) or up.shape != (3,)
            or not np.isfinite(right).all() or not np.isfinite(up).all()):
        raise ValueError('Expected finite camera axes and six normalized operator axes')
    if planar:
        right[2] = up[2] = 0.
    right_length, up_length = np.linalg.norm(right), np.linalg.norm(up)
    if min(right_length, up_length) < 1e-6:
        raise PreflightRejectedError('Camera has no usable table-plane projection; choose WORLD')
    right /= right_length
    up /= up_length
    result = axes.copy()
    result[:3] = axes[0] * up - axes[1] * right + [0., 0., axes[2]]
    result[:3] /= max(1., np.max(np.abs(result[:3])))
    return result
