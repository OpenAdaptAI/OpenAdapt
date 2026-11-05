"""Standard RL environments over OpenAdapt's record-checked apps.

One generic browser environment serves every app. An environment directory
names the app (how to start it and how to reset it), the ``verify.yaml`` that
reads its system of record, and a public task pack. The episode reward comes
from :func:`openadapt_flow.verify_run.verify_run`: the record is read through a
channel the agent never touches, and the verdict (CONFIRMED, REFUTED or
INDETERMINATE) decides the reward, never the screen or the agent's report.

The server speaks the OpenEnv protocol (``reset``/``step``/``state`` over HTTP
and WebSocket, ``openenv.yaml``), and :mod:`openadapt_flow.rl_env.prime`
exposes the same servers as a Prime Intellect ``verifiers`` v1 taskset.

Needs the ``openenv`` extra: ``pip install 'openadapt-flow[openenv]'``.
"""

from __future__ import annotations

#: Version of the observation/action contract below. A change to either is a
#: new version, so a trainer can pin it.
CONTRACT_VERSION = "openadapt.rl-env/v1"

__all__ = ["CONTRACT_VERSION"]
