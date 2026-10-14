"""Standalone GRPO trainer with direct WAA HTTP integration.

No openadapt-ml dependency. Will migrate to openadapt-ml later.
"""

from openadapt_evals.training.standalone.config import TrainingConfig
from openadapt_evals.training.standalone.trainer import GRPOTrainer

__all__ = ["GRPOTrainer", "TrainingConfig"]
