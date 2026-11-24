"""General GUI mechanisms over one neutral view of a screen.

Programs that fill forms keep meeting the same few interface shapes in every
kind of app: a portal, an EMR chart, a spreadsheet grid, a SaaS settings page.
This package handles each shape once, over :mod:`openadapt_flow.gui.model`
(controls with a role, a name, a state and the containers they sit in), so no
mechanism depends on a browser or names an application:

- :mod:`openadapt_flow.gui.steps`: a link, a tab or a button can be a step,
  and the step that commits is the one the demonstration showed writing.

Per-app facts (which control, which words, how many writes) are pack data,
learned from a demonstration or declared by the pack author. The engine holds
no word lists of its own.

Adapters turn a backend's view into a :class:`~openadapt_flow.gui.model.Screen`:
:mod:`openadapt_flow.gui.dom` for a browser page,
:mod:`openadapt_flow.gui.adapters` for an accessibility tree (desktop,
spreadsheet) or a ``form_tx`` snapshot.

Stdlib only, apart from the browser adapter.
"""
