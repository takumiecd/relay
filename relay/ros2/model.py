"""Reference-model FK for observations; MoveIt performs IK via a ROS service.

This local geometric calculation never sends a command and keeps observation /
recording available even when an IK service fails. It is checked against real
MoveIt FK and robot_state_publisher TF in the integration tests.
"""
from pathlib import Path
import hashlib
import xml.etree.ElementTree as ET

import numpy as np
from scipy.spatial.transform import Rotation


ASSETS = Path(__file__).resolve().parent / 'assets'


class ReferenceModel:
    def __init__(self):
        self.urdf = (ASSETS / 'reference_arm.urdf').read_text()
        self.srdf = (ASSETS / 'reference_arm.srdf').read_text()
        self.urdf_sha256 = hashlib.sha256(self.urdf.encode()).hexdigest()
        self.srdf_sha256 = hashlib.sha256(self.srdf.encode()).hexdigest()
        by_child = {j.find('child').get('link'): j for j in ET.fromstring(self.urdf).findall('joint')}
        child = 'tool_link'
        self.chain = []
        while child in by_child:
            j = by_child[child];origin = j.find('origin')
            matrix = np.eye(4)
            if origin is not None:
                matrix[:3, 3] = np.fromstring(origin.get('xyz', '0 0 0'), sep=' ')
                matrix[:3, :3] = Rotation.from_euler('xyz', np.fromstring(origin.get('rpy', '0 0 0'), sep=' ')).as_matrix()
            self.chain.append((j.get('name'), j.get('type'), matrix,
                               np.fromstring(j.find('axis').get('xyz'), sep=' ') if j.find('axis') is not None else None))
            child = j.find('parent').get('link')
        if child != 'world':raise ValueError('Expected a world-rooted reference model')
        self.chain.reverse()

    def pose(self, joint_names, positions):
        values = dict(zip(joint_names, positions));result = np.eye(4)
        for name, kind, origin, axis in self.chain:
            result = result @ origin
            if kind == 'revolute':
                motion = np.eye(4)
                motion[:3, :3] = Rotation.from_rotvec(axis * values[name]).as_matrix()
                result = result @ motion
        return result[:3, 3].copy(), result[:3, :3].copy()
