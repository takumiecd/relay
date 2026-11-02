"""Numeric/JSON snapshots, never pickle; scoped to one live simulator model."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import zipfile

import numpy as np


def save_checkpoint(path, snapshot):
    arrays, seen = {}, {}

    def encode(value):
        if isinstance(value, np.ndarray):
            key = (value.dtype.str, value.shape, hashlib.sha256(value.tobytes()).digest())
            name = seen.get(key)
            if name is None:
                name = f'a{len(arrays)}'
                seen[key] = name
                arrays[name] = value
            return {'array': name}
        if isinstance(value, np.generic):
            return encode(value.item())
        if isinstance(value, dict):
            return {'dict': [[encode(k), encode(v)] for k, v in value.items()]}
        if isinstance(value, (tuple, list)):
            return {'tuple' if isinstance(value, tuple) else 'list': [encode(v) for v in value]}
        if value is None or isinstance(value, (str, bool, float, int)):
            return value
        raise TypeError(f'Unsupported checkpoint field: {type(value).__name__}')

    structure = json.dumps(encode(snapshot), allow_nan=False)
    # Deduplication prevents storing the same camera in raw_obs/cache/observables repeatedly.
    with Path(path).open('xb') as stream, zipfile.ZipFile(
            stream, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
        for name, value in dict(structure=np.array(structure), **arrays).items():
            with archive.open(name + '.npy', 'w', force_zip64=True) as entry:
                np.lib.format.write_array(entry, np.asanyarray(value), allow_pickle=False)


def load_checkpoint(path):
    with np.load(path, allow_pickle=False) as data:
        def decode(value):
            if not isinstance(value, dict):
                return value
            if 'array' in value:
                return data[value['array']].copy()
            if 'dict' in value:
                return {decode(k): decode(v) for k, v in value['dict']}
            if 'tuple' in value:
                return tuple(decode(v) for v in value['tuple'])
            if 'list' in value:
                return [decode(v) for v in value['list']]
            raise ValueError('Invalid checkpoint structure')
        return decode(json.loads(str(data['structure'])))
