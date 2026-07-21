/* Shared, dependency-free renderer for the compiled-program visualizer.
 *
 * ONE renderer, THREE surfaces (see program_graph.css header). Exposes a single
 * pure function:
 *
 *     OpenAdaptProgramGraph.render(spec, container)
 *
 * `spec` is the ProgramGraphSpec emitted by the engine
 * (openadapt_flow.visualize.build_program_graph); `container` is a DOM element.
 * No external libraries, no network, no framework — safe under a strict CSP
 * (the flow CLI inlines this; the Tauri desktop view vendors it verbatim).
 * Cloud reimplements the same layout in React over the same spec shape. */
(function (global) {
  "use strict";

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text; // textContent => no HTML injection
    return e;
  }

  function stat(container, n, label, cls) {
    var s = el("div", "opg-stat" + (cls ? " " + cls : ""));
    s.appendChild(el("div", "n", String(n)));
    s.appendChild(el("div", "l", label));
    container.appendChild(s);
  }

  function chip(text, cls) {
    return el("span", "opg-chip" + (cls ? " " + cls : ""), text);
  }

  function detailRow(parent, key, valueNode) {
    var row = el("div", "row");
    row.appendChild(el("span", "k", key));
    if (typeof valueNode === "string") valueNode = el("span", "v", valueNode);
    else valueNode.classList.add("v");
    row.appendChild(valueNode);
    parent.appendChild(row);
  }

  function renderHeader(spec, root) {
    var b = spec.bundle;
    var head = el("div", "opg-header");
    head.appendChild(el("div", "opg-title", b.name));
    var sub =
      "Compiled program · schema v" +
      b.schema_version +
      " · " +
      (b.is_program ? "program graph" : "linear program") +
      (b.created_at ? " · compiled " + b.created_at.slice(0, 10) : "");
    head.appendChild(el("div", "opg-subtitle", sub));

    var stats = el("div", "opg-stats");
    stat(stats, b.action_count, "steps");
    stat(stats, b.identity_armed_count, "identity gates");
    if (b.identity_unarmed_count)
      stat(stats, b.identity_unarmed_count, "no gate", "warn");
    if (b.irreversible_count)
      stat(stats, b.irreversible_count, "irreversible", "warn");
    if (b.effect_count) stat(stats, b.effect_count, "effect checks");
    if (b.api_binding_count) stat(stats, b.api_binding_count, "API writes");
    if (b.halt_point_count) stat(stats, b.halt_point_count, "halt points", "halt");
    head.appendChild(stats);

    // governance + provenance chips
    var meta = el("div", "opg-meta");
    meta.appendChild(
      chip(
        b.contains_phi ? "contains PHI" : "no plaintext PHI",
        b.contains_phi ? "no-identity" : "identity"
      )
    );
    if (b.encrypted) meta.appendChild(chip("encrypted at rest", "identity"));
    if (b.phi_scrubbed) meta.appendChild(chip("PHI-scrubbed", "identity"));
    var prov = b.provenance || {};
    if (prov.compiler_version)
      meta.appendChild(chip("compiler " + prov.compiler_version));
    if (prov.certified)
      meta.appendChild(
        chip("certified: " + (prov.policy_name || "policy"), "identity")
      );
    else if (prov.certification_status)
      meta.appendChild(chip(prov.certification_status, "warn"));
    head.appendChild(meta);

    // parameters
    if (b.params && b.params.length) {
      var pwrap = el("div", "opg-meta");
      pwrap.appendChild(el("span", "opg-node-action", "Parameters:"));
      b.params.forEach(function (p) {
        var label = p.name + " : " + p.type + (p.required ? "" : " (optional)");
        pwrap.appendChild(chip(label, p.secret ? "secret" : ""));
      });
      head.appendChild(pwrap);
    }
    root.appendChild(head);
  }

  function renderLadder(res) {
    var wrap = el("div", "opg-ladder");
    res.rungs.forEach(function (r) {
      var cls = "opg-rung";
      if (r.present) cls += " present";
      if (r.name === res.top_rung) cls += " top";
      var c = el("span", cls, r.label);
      if (r.present && r.detail) c.title = r.detail;
      wrap.appendChild(c);
    });
    return wrap;
  }

  function renderActionNode(node) {
    var card = el(
      "div",
      "opg-node" + (node.risk === "irreversible" ? " irreversible" : "")
    );
    var head = el("div", "opg-node-head");
    head.appendChild(el("span", "opg-idx", String(node.index + 1)));
    head.appendChild(el("span", "opg-node-title", node.title));
    if (node.action)
      head.appendChild(el("span", "opg-node-action", " " + node.action));
    card.appendChild(head);

    if (node.badges && node.badges.length) {
      var chips = el("div", "opg-chips");
      node.badges.forEach(function (bd) {
        var cls = "";
        if (bd === "irreversible") cls = "irreversible";
        else if (bd === "identity gate") cls = "identity";
        else if (bd === "no identity gate") cls = "no-identity";
        else if (bd === "effect check") cls = "effect";
        else if (bd === "API") cls = "api";
        else if (bd === "secret") cls = "secret";
        chips.appendChild(chip(bd, cls));
      });
      card.appendChild(chips);
    }

    var detail = el("div", "opg-detail");
    if (node.resolution)
      detailRow(detail, "resolve by", renderLadder(node.resolution));
    if (node.identity && node.identity.applicable) {
      if (node.identity.armed) {
        var idv =
          "armed" +
          (node.identity.phi_free ? " · PHI-free template" : "") +
          (node.identity.has_structured ? " · structured" : "") +
          (node.identity.has_identifier_crop ? " · pixel crop" : "");
        detailRow(detail, "identity", idv);
      } else {
        detailRow(
          detail,
          "identity",
          "UNARMED — " + (node.identity.reason || "no identity band")
        );
      }
    }
    if (node.effects && node.effects.length) {
      node.effects.forEach(function (ef) {
        detailRow(detail, "effect", ef.summary);
      });
    }
    if (node.postconditions && node.postconditions.length)
      detailRow(detail, "verify", node.postconditions.join(", "));
    if (node.wait_until) detailRow(detail, "wait until", node.wait_until);
    if (node.guard)
      detailRow(
        detail,
        "guard",
        node.guard + " → " + (node.guard_on_unmet || "halt")
      );
    if (node.param) detailRow(detail, "input", "parameter " + node.param);
    if (detail.childNodes.length) card.appendChild(detail);

    if (node.halts && node.halts.length) {
      var halts = el("div", "opg-halts");
      node.halts.forEach(function (h) {
        halts.appendChild(el("div", "opg-halt-item", h));
      });
      card.appendChild(halts);
    }
    return card;
  }

  function renderTerminalNode(node) {
    var cls = "opg-node terminal";
    if (node.outcome === "success") cls += " ok";
    else if (node.outcome === "halt" || node.outcome === "escalate") cls += " halt";
    var card = el("div", cls);
    card.appendChild(el("div", "opg-node-title", node.title));
    if (node.reason) card.appendChild(el("div", "opg-reason", node.reason));
    return card;
  }

  function renderControlNode(node) {
    var card = el("div", "opg-node");
    var head = el("div", "opg-node-head");
    head.appendChild(el("span", "opg-idx", String(node.index + 1)));
    head.appendChild(el("span", "opg-node-title", node.title));
    head.appendChild(el("span", "opg-node-action", node.kind));
    card.appendChild(head);
    if (node.badges && node.badges.length) {
      var chips = el("div", "opg-chips");
      node.badges.forEach(function (bd) {
        chips.appendChild(chip(bd));
      });
      card.appendChild(chips);
    }
    return card;
  }

  function connector(label, branch) {
    var c = el("div", "opg-connector" + (branch ? " branch" : ""));
    c.appendChild(el("div", "line"));
    if (label) c.appendChild(el("div", "lbl", label));
    c.appendChild(el("div", "line"));
    return c;
  }

  function renderLegend(root) {
    var items = [
      ["identity", "identity gate armed"],
      ["no-identity", "no identity gate"],
      ["irreversible", "irreversible write"],
      ["halt", "fail-safe halt point"],
    ];
    var leg = el("div", "opg-legend");
    items.forEach(function (it) {
      var wrap = el("div", "item");
      var sw = el("span", "opg-swatch opg-chip " + it[0]);
      sw.textContent = "";
      wrap.appendChild(sw);
      wrap.appendChild(document.createTextNode(it[1]));
      leg.appendChild(wrap);
    });
    root.appendChild(leg);
  }

  function render(spec, container) {
    container.innerHTML = "";
    var root = el("div", "opg-root");
    renderHeader(spec, root);

    var flow = el("div", "opg-flow");
    // Build an index of outgoing edges for linear sequencing / labels.
    var outBySource = {};
    (spec.edges || []).forEach(function (e) {
      (outBySource[e.source] = outBySource[e.source] || []).push(e);
    });

    var nodes = spec.nodes || [];
    nodes.forEach(function (node, i) {
      var card;
      if (node.kind === "terminal") card = renderTerminalNode(node);
      else if (node.kind === "action") card = renderActionNode(node);
      else card = renderControlNode(node);
      flow.appendChild(card);

      // connector to the next node in document order (linear default). For a
      // branch/loop, surface the first outgoing edge's label so multi-way
      // structure is legible even without a full 2-D graph layout.
      if (i < nodes.length - 1) {
        var edges = outBySource[node.id] || [];
        var isBranch = edges.some(function (e) {
          return e.kind === "branch" || e.kind === "loop_body";
        });
        var label = "";
        if (edges.length === 1 && edges[0].label) label = edges[0].label;
        else if (isBranch) label = edges.length + " branches";
        flow.appendChild(connector(label, isBranch));
      }
    });
    root.appendChild(flow);
    renderLegend(root);
    container.appendChild(root);
    return root;
  }

  var api = { render: render };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  global.OpenAdaptProgramGraph = api;
})(typeof window !== "undefined" ? window : this);
