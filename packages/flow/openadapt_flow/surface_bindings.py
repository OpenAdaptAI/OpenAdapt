"""Bind live backends to a bundle's backend surfaces at replay time.

A multi-surface bundle (docs/MULTI_SURFACE.md) declares its surfaces. Browser
tabs and popups its own steps open need nothing from the caller. A BACKEND
surface (a native app, an RDP/Citrix window, a second web app) is supplied by
the caller, keyed by surface id. This module is the one generic way the CLI
does that, with no per-workflow code:

- ``--surface NAME=SPEC`` (repeatable), where ``SPEC`` is ``KIND``,
  ``KIND:TARGET``, or just ``TARGET``. The kind defaults to the kind the
  bundle declares for ``NAME``; a different kind is refused. ``TARGET`` fills
  the kind's one primary target field (see :data:`TARGET_FIELD`);
- ``surfaces:`` in a deployment config: a map from surface id to a full
  ``backend`` section, for any field the one-token target cannot carry
  (tokens, window titles, readiness text, markers).

A flag is applied over the config entry for the same surface. Nothing is
inferred: a backend surface with neither stays unbound, and the run halts at
the switch to it with a reason that names the flag.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, Mapping, Optional, Sequence

if TYPE_CHECKING:  # pragma: no cover
    from openadapt_flow.deployment import BackendConfig
    from openadapt_flow.ir import Workflow

#: Surface kind -> the ``BackendConfig`` field one ``TARGET`` token fills.
TARGET_FIELD: dict[str, str] = {
    "web": "url",
    "windows": "agent_url",
    "macos": "macos_app",
    "linux": "linux_app",
    "rdp": "rdp_host",
    "citrix": "rdp_window",
}

_KINDS = frozenset(TARGET_FIELD)


class SurfaceBindingError(ValueError):
    """A ``--surface`` flag or ``surfaces:`` entry cannot be bound."""


def parse_surface_flag(text: str) -> tuple[str, Optional[str], Optional[str]]:
    """Split ``NAME=SPEC`` into ``(name, kind or None, target or None)``.

    ``SPEC`` is ``KIND``, ``KIND:TARGET``, or ``TARGET`` (kind from the
    bundle). A target that itself contains a colon (``http://host:5001``) is
    read as a target unless the text before its first colon is a surface
    kind.
    """

    name, sep, spec = text.partition("=")
    name = name.strip()
    spec = spec.strip()
    if not sep or not name or not spec:
        raise SurfaceBindingError(
            f"--surface expects NAME=KIND[:TARGET] or NAME=TARGET, got {text!r}"
        )
    if spec.lower() in _KINDS:
        return name, spec.lower(), None
    head, colon, rest = spec.partition(":")
    if colon and head.lower() in _KINDS:
        return name, head.lower(), (rest.strip() or None)
    return name, None, spec


def resolve_surface_configs(
    workflow: "Workflow",
    flags: Sequence[str] = (),
    configured: Optional[Mapping[str, "BackendConfig"]] = None,
) -> dict[str, "BackendConfig"]:
    """Resolve one ``BackendConfig`` per bound backend surface.

    Refuses a name the bundle does not declare, a primary or page surface (the
    primary is the run's own ``--backend``; pages are opened by steps), and a
    kind other than the declared one.
    """

    from openadapt_flow.deployment import BackendConfig

    specs = {spec.id: spec for spec in workflow.surfaces}
    primary = workflow.surfaces[0].id if workflow.surfaces else None

    def _check(name: str) -> Any:
        spec = specs.get(name)
        if spec is None:
            declared = ", ".join(s.id for s in workflow.surfaces[1:] if not s.is_page)
            raise SurfaceBindingError(
                f"surface {name!r} is not declared by this bundle"
                + (f" (backend surfaces: {declared})" if declared else "")
            )
        if name == primary:
            raise SurfaceBindingError(
                f"surface {name!r} is the primary surface; select its backend "
                "with --backend / backend: instead"
            )
        if spec.is_page:
            raise SurfaceBindingError(
                f"surface {name!r} is a browser page a step opens; it needs no binding"
            )
        return spec

    resolved: dict[str, BackendConfig] = {}
    for name, cfg in (configured or {}).items():
        spec = _check(name)
        cfg = BackendConfig.model_validate(cfg)
        if "kind" not in cfg.model_fields_set:
            cfg = cfg.model_copy(update={"kind": spec.kind})
        resolved[name] = cfg
    for text in flags:
        name, kind, target = parse_surface_flag(text)
        spec = _check(name)
        base = resolved.get(name) or BackendConfig(kind=spec.kind)
        updates: dict[str, Any] = {"kind": kind or base.kind}
        if target is not None:
            updates[TARGET_FIELD[str(updates["kind"]).lower()]] = target
        resolved[name] = base.model_copy(update=updates)
    for name, cfg in resolved.items():
        kind = str(cfg.kind).strip().lower()
        if kind != specs[name].kind:
            raise SurfaceBindingError(
                f"surface {name!r} is declared {specs[name].kind}, but it was "
                f"bound to a {kind} backend"
            )
    return resolved


def unbound_backend_surfaces(
    workflow: "Workflow", bound: Mapping[str, Any]
) -> list[str]:
    """Backend surfaces of ``workflow`` with no binding, in declared order."""

    return [
        spec.id
        for spec in workflow.surfaces[1:]
        if not spec.is_page and spec.id not in bound
    ]


def build_surface_backends(
    configs: Mapping[str, "BackendConfig"],
    *,
    new_page: Optional[Callable[[str], Any]] = None,
    build: Optional[Callable[..., Any]] = None,
) -> dict[str, Any]:
    """Construct each bound backend through the ordinary backend factory.

    A ``web`` backend surface is a second app in the run's own browser:
    ``new_page(url)`` opens and navigates that page (the CLI passes a function
    that opens it in the launched browser context, so the surface router
    watches it like every other page).
    """

    if build is None:
        from openadapt_flow.backends.factory import build_backend

        build = build_backend
    backends: dict[str, Any] = {}
    for name, cfg in configs.items():
        if str(cfg.kind).strip().lower() == "web":
            if not cfg.url:
                raise SurfaceBindingError(
                    f"web surface {name!r} needs a URL (--surface {name}=web:URL)"
                )
            if new_page is None:
                raise SurfaceBindingError(
                    f"web surface {name!r} needs a browser; bind it on a run "
                    "whose primary surface is the browser"
                )
            backends[name] = build(cfg, page=new_page(cfg.url))
        else:
            backends[name] = build(cfg)
    return backends
