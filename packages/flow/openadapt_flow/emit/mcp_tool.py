"""Emit a workflow bundle as a standalone MCP server (``server.py``).

The generated source exposes the workflow as a single FastMCP tool with
typed parameters. Generation is pure string templating: this module never
imports ``mcp`` — the generated file does, at *its* runtime.

The workflow bundle is COPIED next to the generated file and referenced
relative to ``__file__``, so the emitted directory is self-contained and
portable across machines. Each server gets its own copy: ``server.py`` uses
``bundle/`` and any other file name ``<stem>.py`` uses ``<stem>_bundle/``, so
two servers emitted into one directory never share or merge a bundle.
"""

from __future__ import annotations

import ast
import re
import shutil
from pathlib import Path

from openadapt_flow.emit.mcp_governed import SERVER_TEMPLATE
from openadapt_flow.emit.mcp_sdk import server_import
from openadapt_flow.emit.python_names import PythonNames
from openadapt_flow.emit.skill import bundle_surface, declared_params
from openadapt_flow.ir import Workflow

_BUNDLE_SUBDIR = "bundle"
_DEFAULT_SERVER_NAME = "server.py"


def _identifier(name: str) -> str:
    """Coerce an arbitrary name into a valid Python identifier."""
    ident = re.sub(r"[^0-9a-zA-Z_]+", "_", name).strip("_").lower()
    if not ident:
        ident = "workflow"
    if ident[0].isdigit():
        ident = f"w_{ident}"
    return ident


def _clean_line(text: str) -> str:
    """Make text safe to embed inside a generated docstring."""
    return text.replace("\\", " ").replace('"""', "'''").replace("\n", " ")


def emit_mcp_server(bundle_dir: Path | str, out_path: Path | str) -> Path:
    """Generate a standalone, portable FastMCP ``server.py`` for the bundle.

    The generated module defines one tool named after the workflow, with
    one required ``str`` argument per non-secret workflow parameter. A web
    bundle's target, like every other surface's target and readback, comes
    only from the server's deployment configuration. Startup requires
    ``--config``, an explicit Standard/Regulated ``--profile``, and the
    existing ``--qualification-authority-file``. A non-null legacy ``url``
    tool argument refuses before child launch. A recorded example value is
    never a default: it would leak the recorded value into the generated source and
    write it to the wrong record when a caller omits the argument. The source is
    validated with :func:`ast.parse` before being written. The workflow
    bundle is copied next to the generated file, to ``bundle/`` for
    ``server.py`` and to ``<stem>_bundle/`` for any other file name, and is
    referenced relative to that file, so the directory can be shipped as-is.
    An earlier copy of the same workflow there is replaced, not merged, so no
    stale template survives. A copy of a different workflow, or any other
    content, is never overwritten: emission raises :class:`FileExistsError`.

    Args:
        bundle_dir: Workflow bundle directory (contains ``workflow.json``).
        out_path: Destination path for the generated ``server.py``.

    Returns:
        Path to the written server source file.

    Raises:
        FileExistsError: The bundle directory for ``out_path`` already holds
            something other than a copy of this workflow.
    """
    bundle = Path(bundle_dir).resolve()
    workflow = Workflow.load(bundle)
    out = Path(out_path)
    bundle_subdir = _bundle_subdir_for(out)

    # Reserve every executable name in both template scopes before allocating
    # injective aliases. Original workflow names remain dictionary keys.
    template_values = dict(
        sdk_import=server_import("FastMCP", "mcp_import_error"),
        name="workflow",
        bundle_subdir="bundle",
        surface="web",
        func_name="_emitted_tool",
        param_sig_first="",
        param_docs="",
        params_dict="{}",
        docstring="workflow",
        bundle_digest="",
        workflow_contract="",
    )
    namespace = PythonNames.from_source(SERVER_TEMPLATE.format(**template_values))
    func_name = namespace.allocate(f"run_{_identifier(workflow.name)}")
    param_names = {
        name: namespace.allocate(_identifier(name))
        for name in declared_params(workflow)
    }

    param_sig = "".join(f", {ident}: str" for ident in param_names.values())
    if param_names:
        params_dict = (
            "{"
            + ", ".join(f"{name!r}: {ident}" for name, ident in param_names.items())
            + "}"
        )
        param_docs = "".join(
            f"        {ident}: Value for the {_clean_line(name)!r} "
            f"workflow parameter.\n"
            for name, ident in param_names.items()
        )
    else:
        params_dict = "{}"
        param_docs = ""

    from openadapt_flow.jit.lifecycle import draft_label

    docstring = _clean_line(
        f"Replay the {workflow.name!r} workflow "
        f"({len(workflow.steps)} steps) against a live app."
    )
    if draft_label(bundle):
        docstring = _clean_line(
            "DRAFT, not admitted: this tool refuses to run until a person "
            "reviews, canaries and admits the bundle (openadapt-flow jit). " + docstring
        )

    from openadapt_flow.qualification import workflow_contract_sha256

    source = SERVER_TEMPLATE.format(
        sdk_import=server_import("FastMCP", "mcp_import_error"),
        name=workflow.name,
        bundle_subdir=bundle_subdir,
        surface=bundle_surface(workflow),
        func_name=func_name,
        param_sig_first=(param_sig.removeprefix(", ") + ", " if param_names else ""),
        param_docs=param_docs,
        params_dict=params_dict,
        docstring=docstring,
        bundle_digest=workflow.manifest.content_digest if workflow.manifest else "",
        workflow_contract=workflow_contract_sha256(workflow),
    )

    # Guarantee the emitted source is syntactically valid Python.
    ast.parse(source)

    out.parent.mkdir(parents=True, exist_ok=True)
    bundle_copy = out.parent / bundle_subdir
    if bundle_copy.resolve() != bundle:
        _replace_bundle_copy(bundle, bundle_copy, workflow.name)
    out.write_text(source, encoding="utf-8")
    return out


def _bundle_subdir_for(out: Path) -> str:
    """Name the bundle directory that belongs to one emitted server file."""
    if out.name == _DEFAULT_SERVER_NAME:
        return _BUNDLE_SUBDIR
    return f"{out.stem}_{_BUNDLE_SUBDIR}"


def _replace_bundle_copy(source: Path, target: Path, workflow_name: str) -> None:
    """Copy ``source`` to ``target``, replacing an earlier copy of this workflow.

    Copying over an existing directory would merge the two bundles: the
    server would load whichever ``workflow.json`` was written last and keep
    templates from both. Only an earlier copy of the same workflow, or an
    empty directory, is replaced. Anything else raises, because deleting it
    could destroy someone's bundle and keeping it would run the wrong
    workflow.
    """
    if target.is_symlink() or (target.exists() and not target.is_dir()):
        raise FileExistsError(
            f"{target} exists and is not a bundle directory; choose another "
            "--out file name or move it away"
        )
    if target.is_dir():
        existing = _copied_workflow_name(target)
        if existing != workflow_name and any(target.iterdir()):
            held = f"the {existing!r} workflow" if existing else "other files"
            raise FileExistsError(
                f"{target} already holds {held}; emitting {workflow_name!r} "
                "there would replace it. Choose another --out file name or "
                "remove that directory"
            )
        if source.is_relative_to(target.resolve()):
            raise FileExistsError(
                f"{target} contains the source bundle {source}; choose "
                "another --out location"
            )
        shutil.rmtree(target)
    shutil.copytree(source, target)


def _copied_workflow_name(directory: Path) -> str | None:
    """Return the workflow name stored in ``directory``, if it holds one."""
    if not (directory / "workflow.json").is_file():
        return None
    try:
        return Workflow.load(directory).name
    except Exception:  # noqa: BLE001 - an unreadable bundle is not ours to replace
        return None
