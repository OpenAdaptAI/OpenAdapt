"""Pack data for the general GUI mechanisms (``openadapt_flow.gui``) in a form pack.

A form pack opts into each mechanism with a :class:`GuiMechanismsSpec` in
``FormPack.gui``. Every field is data: which control, which words a pack
author pinned, how many writes the demonstration saw. Nothing here names an
application, and a pack without ``gui`` behaves exactly as before.

- :class:`GuiStepSpec`: the control that moves a wizard screen on, when it
  is a link, a tab or a button the form surface does not list. The step is
  taken with :func:`openadapt_flow.gui.steps.take_step` and must navigate
  without writing.
- :class:`GuiAttestationSpec`: a checkbox on the commit screen that states
  something. It is ticked only on an
  :class:`~openadapt_flow.gui.attestations.AttestationGrant` given to the run,
  never through an intake or derived fact.
- :class:`GuiRepeatSpec`: an "add another" group on one screen, filled from a
  list in the intake.
- :class:`GuiHubSpec`: a hub screen whose sections open in dialogs, walked
  section by section with completion read from each card, then left through
  a step.
- ``stop_on_blank_choice``: a visible choice (a select or a radio group) that
  no binding covers, the pack does not list as left alone, and that is still
  blank stops before the screen is left. Without it such a question is
  skipped silently.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from openadapt_flow.form_tx.types import FieldCodecSpec
from openadapt_flow.gui.model import ControlSpec

__all__ = [
    "GuiAttestationSpec",
    "GuiControlRef",
    "GuiHubSection",
    "GuiHubSpec",
    "GuiMechanismsSpec",
    "GuiRepeatSlot",
    "GuiRepeatSpec",
    "GuiStepSpec",
]


class GuiControlRef(BaseModel):
    """One control, named the way :class:`~openadapt_flow.gui.model.ControlSpec` names it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    role: str = Field(min_length=1)
    name: str = Field(min_length=1)
    containers: tuple[str, ...] = ()
    target: Optional[str] = None

    def spec(self, *, dialog: Optional[bool] = None) -> ControlSpec:
        return ControlSpec(self.role, self.name, self.containers, self.target, dialog)


class GuiStepSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    screen_id: str = Field(min_length=1)
    control: GuiControlRef
    role: Literal["navigate", "in_place"] = "navigate"


class GuiAttestationSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    attestation_id: str = Field(min_length=1)
    screen_id: str = Field(min_length=1)
    control: GuiControlRef
    statement_sha256: str = Field(min_length=64, max_length=64)
    statement_from: Literal["name", "container"] = "name"


class GuiRepeatSlot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    role: str = Field(min_length=1)
    name: str = Field(min_length=1)
    item_key: str = Field(min_length=1)
    codec: FieldCodecSpec = Field(default_factory=FieldCodecSpec)


class GuiRepeatSpec(BaseModel):
    """An "add another" group. ``intake_key`` names the intake list; each
    item is a mapping whose ``item_key`` values feed the slots.
    ``instance_pattern`` is a regular expression an instance's container name
    must match (``^Traveller \\d+$``), so a same-named field elsewhere on the
    screen is never taken for an instance."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    group_id: str = Field(min_length=1)
    screen_id: str = Field(min_length=1)
    intake_key: str = Field(min_length=1)
    add: GuiControlRef
    instance_kind: str = "region"
    instance_pattern: str = ""
    slots: tuple[GuiRepeatSlot, ...] = Field(min_length=1)
    add_writes: int = Field(default=0, ge=0)
    max_instances: int = Field(default=20, ge=1, le=200)


class GuiHubSection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    section_id: str = Field(min_length=1)
    opener: GuiControlRef
    dialog_name: str = Field(min_length=1)
    save: GuiControlRef
    card: str = Field(min_length=1)
    done_tokens: tuple[str, ...] = Field(min_length=1)
    pending_tokens: tuple[str, ...] = ()
    save_writes: int = Field(default=1, ge=0)


class GuiHubSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    screen_id: str = Field(min_length=1)
    sections: tuple[GuiHubSection, ...] = Field(min_length=1)
    #: The step that leaves the hub once every section reads done.
    leave: GuiControlRef


class GuiMechanismsSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    steps: tuple[GuiStepSpec, ...] = ()
    attestations: tuple[GuiAttestationSpec, ...] = ()
    repeats: tuple[GuiRepeatSpec, ...] = ()
    hubs: tuple[GuiHubSpec, ...] = ()
    stop_on_blank_choice: bool = False
