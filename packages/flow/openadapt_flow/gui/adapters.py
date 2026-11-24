"""Turn other backends' views into a :class:`~openadapt_flow.gui.model.Screen`.

- :func:`screen_from_tree`: an accessibility tree (UI Automation, AX, AT-SPI,
  or a spreadsheet's cell grid exported as one). Each node is a mapping with
  ``role``, ``name`` and optional ``handle``, ``value``, ``checked``,
  ``disabled``, ``readonly``, ``target``, ``text`` and ``children``. Nodes
  whose role is a container role (:data:`CONTAINER_ROLES`) become containers
  of the controls under them; a ``dialog`` node also becomes a
  :class:`~openadapt_flow.gui.model.Dialog`.
- :func:`screen_from_form_snapshot`: a ``form_tx`` ``FormSnapshot``, read by
  attribute so this module does not import ``form_tx``. That snapshot keeps
  section and row text, not container handles, so the containers it gives are
  named by their text and carry no structure beyond it.

Role names are mapped through :data:`TREE_ROLES`; an unknown role is
``other`` and never acts as a control a mechanism can use.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from openadapt_flow.gui.model import ROLES, Container, Control, Dialog, Screen

__all__ = [
    "CONTAINER_ROLES",
    "TREE_ROLES",
    "screen_from_form_snapshot",
    "screen_from_tree",
]

#: Accessibility role names from common trees, mapped to :data:`ROLES`.
TREE_ROLES: Mapping[str, str] = {
    "edit": "textbox",
    "text field": "textbox",
    "textfield": "textbox",
    "entry": "textbox",
    "searchbox": "textbox",
    "spinbutton": "textbox",
    "check box": "checkbox",
    "check_box": "checkbox",
    "toggle button": "switch",
    "radio button": "radio",
    "radiobutton": "radio",
    "combo box": "combobox",
    "popupbutton": "combobox",
    "list": "listbox",
    "push button": "button",
    "hyperlink": "link",
    "tab item": "tab",
    "tabitem": "tab",
    "page tab": "tab",
    "menu item": "menuitem",
    "dataitem": "cell",
    "data item": "cell",
    "gridcell": "cell",
    "table cell": "cell",
    "static text": "text",
    "statictext": "text",
    "label": "text",
}

#: Tree roles that hold controls rather than being one, and their kind.
CONTAINER_ROLES: Mapping[str, str] = {
    "group": "group",
    "grouping": "group",
    "pane": "region",
    "region": "region",
    "section": "region",
    "form": "form",
    "dialog": "dialog",
    "window": "region",
    "row": "row",
    "dataitem row": "row",
    "list item": "item",
    "listitem": "item",
    "table": "region",
    "grid": "region",
    "sheet": "region",
}


def _role(raw: object) -> str:
    key = " ".join(str(raw or "").split()).casefold()
    if key in ROLES:
        return key
    return TREE_ROLES.get(key, "other")


def screen_from_tree(
    root: Mapping[str, Any],
    *,
    location: str = "",
    title: str = "",
    complete: bool = True,
) -> Screen:
    """Flatten ``root`` into a :class:`Screen`. A node without ``handle`` gets
    its index path (``0/2/1``), which is stable only while the tree is."""

    controls: list[Control] = []
    dialogs: list[Dialog] = []
    texts: list[str] = []

    def walk(
        node: Mapping[str, Any],
        path: str,
        boxes: tuple[Container, ...],
        dialog: str,
    ) -> None:
        handle = str(node.get("handle") or path)
        raw_role = " ".join(str(node.get("role") or "").split()).casefold()
        name = str(node.get("name") or "")
        kind = CONTAINER_ROLES.get(raw_role)
        if node.get("text"):
            texts.append(str(node["text"]))
        if kind is not None:
            box = Container(handle, kind, name, str(node.get("text") or name))
            boxes = boxes + (box,)
            if kind == "dialog" and raw_role == "dialog":
                dialogs.append(Dialog(handle, name, str(node.get("text") or "")))
                dialog = handle
        else:
            role = _role(raw_role)
            if role != "other":
                checked = node.get("checked")
                controls.append(
                    Control(
                        handle=handle,
                        role=role,
                        name=name,
                        value=str(node.get("value") or ""),
                        checked=None if checked is None else bool(checked),
                        disabled=bool(node.get("disabled")),
                        readonly=bool(node.get("readonly")),
                        required=bool(node.get("required")),
                        containers=boxes,
                        dialog=dialog,
                        target=str(node.get("target") or ""),
                        error=str(node.get("error") or ""),
                    )
                )
        for i, child in enumerate(node.get("children") or ()):
            walk(child, f"{path}/{i}", boxes, dialog)

    walk(root, "0", (), "")
    return Screen(
        controls=tuple(controls),
        dialogs=tuple(dialogs),
        location=location,
        title=title or str(root.get("name") or ""),
        text="\n".join(texts),
        complete=complete,
    )


def _snapshot_role(ctrl: Any) -> str:
    role = _role(getattr(ctrl, "role", ""))
    if role != "other":
        return role
    tag = str(getattr(ctrl, "tag", "")).casefold()
    kind = str(getattr(ctrl, "input_type", "")).casefold()
    if tag == "a":
        return "link"
    if tag == "button" or kind in ("submit", "button", "reset", "image"):
        return "button"
    if tag == "select":
        return "combobox"
    if kind in ("checkbox", "radio"):
        return kind
    if tag in ("input", "textarea"):
        return "textbox"
    return "other"


def screen_from_form_snapshot(snapshot: Any) -> Screen:
    """A ``form_tx`` snapshot as a :class:`Screen`. Containers are the
    control's section text and row text, named by that text."""

    controls = []
    for ctrl in getattr(snapshot, "controls", ()) or ():
        if not getattr(ctrl, "visible", True) or getattr(ctrl, "is_secret", False):
            continue
        boxes: list[Container] = []
        section = str(getattr(ctrl, "section_text", "") or "")
        if section:
            boxes.append(Container(f"section:{section}", "region", section, section))
        row = str(getattr(ctrl, "row_text", "") or "")
        if row:
            boxes.append(Container(f"row:{row}", "row", "", row))
        modal = str(getattr(ctrl, "modal_container", "") or "")
        checked: Optional[bool] = getattr(ctrl, "checked", None)
        controls.append(
            Control(
                handle=str(getattr(ctrl, "dom_selector")),
                role=_snapshot_role(ctrl),
                name=str(getattr(ctrl, "label_text", "") or ""),
                value=str(getattr(ctrl, "current_value", "") or ""),
                checked=checked,
                disabled=bool(getattr(ctrl, "disabled", False)),
                readonly=bool(getattr(ctrl, "readonly", False)),
                required=bool(getattr(ctrl, "required", False)),
                containers=tuple(boxes),
                dialog=modal,
                error=str(getattr(ctrl, "error_text", "") or ""),
            )
        )
    dialogs = tuple(
        Dialog(
            str(getattr(m, "container_selector", "")),
            str(getattr(m, "modal_id", "")),
            str(getattr(m, "message_text", "") or ""),
        )
        for m in getattr(snapshot, "active_modals", ()) or ()
    )
    return Screen(
        controls=tuple(controls),
        dialogs=dialogs,
        location=str(getattr(snapshot, "url_origin_and_path", "") or ""),
        title=str(getattr(snapshot, "title", "") or ""),
        text=str(getattr(snapshot, "page_text", "") or ""),
        errors=tuple(getattr(snapshot, "banner_errors", ()) or ()),
    )
