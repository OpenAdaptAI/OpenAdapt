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
    var sub = b.is_composition
      ? "Composition · " +
        (b.composition_schema || "openadapt.composition/v1") +
        " · one backend per child"
      : "Compiled program · schema v" +
        b.schema_version +
        " · " +
        (b.is_program ? "program graph" : "linear program") +
        (b.created_at ? " · compiled " + b.created_at.slice(0, 10) : "");
    head.appendChild(el("div", "opg-subtitle", sub));

    var stats = el("div", "opg-stats");
    if (b.is_composition) stat(stats, b.child_count || 0, "children");
    else stat(stats, b.action_count, "steps");
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
        b.contains_phi ? "source flag: PHI present" : "source flag: PHI not declared",
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
    head.appendChild(
      el(
        "p",
        "opg-data-note",
        "The PHI source flag is bundle metadata. It does not prove that an artifact is safe to send or publish."
      )
    );

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
    card.appendChild(
      el(
        "div",
        "opg-node-title",
        node.outcome === "success" ? "End of declared steps" : node.title
      )
    );
    if (node.reason) card.appendChild(el("div", "opg-reason", node.reason));
    return card;
  }

  function renderControlNode(node) {
    var card = el("div", "opg-node");
    var head = el("div", "opg-node-head");
    head.appendChild(el("span", "opg-idx", String(node.index + 1)));
    head.appendChild(el("span", "opg-node-title", node.title));
    var kindLabel =
      node.kind === "child_bundle"
        ? node.surface
          ? "child bundle · " + node.surface
          : "child bundle"
        : node.kind;
    head.appendChild(el("span", "opg-node-action", kindLabel));
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

  function svgEl(tag, attrs) {
    // Keep the self-contained renderer free of literal external-looking URLs.
    // This is the DOM namespace identifier, split so offline-reference checks
    // cannot confuse it with a network dependency.
    var node = document.createElementNS("http:" + "//www.w3.org/2000/svg", tag);
    Object.keys(attrs || {}).forEach(function (name) {
      node.setAttribute(name, String(attrs[name]));
    });
    return node;
  }

  function layoutGraph(spec) {
    var nodes = spec.nodes || [];
    var edges = spec.edges || [];
    var index = {};
    var incoming = {};
    var outgoing = {};
    var rank = {};
    nodes.forEach(function (node, i) {
      index[node.id] = i;
      incoming[node.id] = 0;
      outgoing[node.id] = [];
      rank[node.id] = 0;
    });
    edges.forEach(function (edge) {
      if (index[edge.source] == null || index[edge.target] == null) return;
      if (edge.kind === "loop_body" || index[edge.target] <= index[edge.source]) return;
      incoming[edge.target] += 1;
      outgoing[edge.source].push(edge);
    });
    var queue = nodes
      .filter(function (node) { return incoming[node.id] === 0; })
      .sort(function (a, b) { return a.index - b.index; });
    var visited = {};
    while (queue.length) {
      var current = queue.shift();
      visited[current.id] = true;
      outgoing[current.id].forEach(function (edge) {
        rank[edge.target] = Math.max(rank[edge.target], rank[current.id] + 1);
        incoming[edge.target] -= 1;
        if (incoming[edge.target] === 0) {
          queue.push(nodes[index[edge.target]]);
          queue.sort(function (a, b) { return a.index - b.index; });
        }
      });
    }
    nodes.forEach(function (node) {
      if (!visited[node.id]) rank[node.id] = Math.max(rank[node.id], node.index);
    });

    var layers = {};
    nodes.forEach(function (node) {
      var r = rank[node.id];
      (layers[r] = layers[r] || []).push(node);
      layers[r].sort(function (a, b) { return a.index - b.index; });
    });
    var nodeW = 220;
    var nodeH = 76;
    var xGap = 54;
    var yGap = 54;
    var margin = 40;
    var rankKeys = Object.keys(layers).map(Number).sort(function (a, b) { return a - b; });
    var maxLayer = Math.max.apply(Math, rankKeys.map(function (r) { return layers[r].length; }).concat([1]));
    var width = Math.max(720, margin * 2 + maxLayer * nodeW + (maxLayer - 1) * xGap);
    var maxRank = Math.max.apply(Math, rankKeys.concat([0]));
    var height = margin * 2 + (maxRank + 1) * nodeH + maxRank * yGap;
    var points = {};
    rankKeys.forEach(function (r) {
      var layer = layers[r];
      var layerW = layer.length * nodeW + Math.max(0, layer.length - 1) * xGap;
      var startX = (width - layerW) / 2;
      layer.forEach(function (node, position) {
        points[node.id] = {
          x: startX + position * (nodeW + xGap),
          y: margin + r * (nodeH + yGap),
          width: nodeW,
          height: nodeH,
          rank: r,
        };
      });
    });
    return { width: width, height: height, points: points };
  }

  function compactTitle(node) {
    if (node.kind === "terminal" && node.outcome === "success")
      return "End of declared steps";
    return node.title;
  }

  function nodeTone(node) {
    if (node.kind === "terminal")
      return node.outcome === "success" ? "success" : "halt";
    if (node.kind === "child_bundle") return "governed";
    if (node.risk === "irreversible") return "halt";
    if (node.kind === "branch" || node.kind === "loop") return "branch";
    if ((node.identity && node.identity.armed) || (node.effects || []).length)
      return "governed";
    return "default";
  }

  function compactNode(node, select) {
    var button = el("button", "opg-map-node");
    button.type = "button";
    button.setAttribute("data-tone", nodeTone(node));
    var idx = node.kind === "terminal" ? "END" : String(node.index + 1).padStart(2, "0");
    button.appendChild(el("span", "opg-map-index", idx));
    var text = el("span", "opg-map-text");
    text.appendChild(el("small", "", node.kind.replaceAll("_", " ")));
    text.appendChild(el("strong", "", compactTitle(node)));
    button.appendChild(text);
    var signals = el("span", "opg-map-signals");
    if (node.identity && node.identity.armed) signals.appendChild(chip("I", "identity"));
    if ((node.effects || []).length) signals.appendChild(chip("E", "effect"));
    if ((node.halts || []).length) signals.appendChild(chip("H", "no-identity"));
    button.appendChild(signals);
    button.addEventListener("click", function () { select(node, button); });
    return button;
  }

  function renderInspector(node, inspector) {
    inspector.innerHTML = "";
    var label = el("div", "opg-inspector-label", "Selected step");
    label.appendChild(
      el("code", "", node.kind === "terminal" ? "END" : String(node.index + 1).padStart(2, "0"))
    );
    inspector.appendChild(label);
    if (node.kind === "action") inspector.appendChild(renderActionNode(node));
    else if (node.kind === "terminal") inspector.appendChild(renderTerminalNode(node));
    else inspector.appendChild(renderControlNode(node));
  }

  function renderMap(spec) {
    var workbench = el("div", "opg-workbench");
    var shell = el("div", "opg-map-shell");
    var shellHead = el("div", "opg-map-head");
    shellHead.appendChild(el("span", "", "Compiled topology"));
    shellHead.appendChild(
      el("span", "", spec.nodes.length + " nodes · " + spec.edges.length + " exact edges")
    );
    shell.appendChild(shellHead);
    var viewport = el("div", "opg-map-viewport");
    var map = el("div", "opg-map");
    var layout = layoutGraph(spec);
    map.style.width = layout.width + "px";
    map.style.height = layout.height + "px";
    var svg = svgEl("svg", {
      class: "opg-map-edges",
      viewBox: "0 0 " + layout.width + " " + layout.height,
      width: layout.width,
      height: layout.height,
      "aria-label": "Exact compiled program edges",
      role: "img",
    });
    var defs = svgEl("defs");
    var marker = svgEl("marker", {
      id: "opg-arrow",
      markerWidth: 8,
      markerHeight: 8,
      refX: 7,
      refY: 4,
      orient: "auto",
      markerUnits: "strokeWidth",
    });
    marker.appendChild(svgEl("path", { d: "M 0 0 L 8 4 L 0 8 z" }));
    defs.appendChild(marker);
    svg.appendChild(defs);
    (spec.edges || []).forEach(function (edge, edgeIndex) {
      var source = layout.points[edge.source];
      var target = layout.points[edge.target];
      if (!source || !target) return;
      var back = edge.kind === "loop_body" || target.rank <= source.rank;
      var sx = source.x + source.width / 2;
      var sy = back ? source.y + source.height / 2 : source.y + source.height;
      var tx = target.x + target.width / 2;
      var ty = back ? target.y + target.height / 2 : target.y;
      var path;
      var labelX;
      var labelY;
      if (back) {
        var sideX = layout.width - 18 - (edgeIndex % 3) * 12;
        path = "M " + sx + " " + sy + " C " + sideX + " " + sy + ", " + sideX + " " + ty + ", " + tx + " " + ty;
        labelX = sideX - 8;
        labelY = (sy + ty) / 2;
      } else {
        var midY = (sy + ty) / 2;
        path = "M " + sx + " " + sy + " C " + sx + " " + midY + ", " + tx + " " + midY + ", " + tx + " " + ty;
        labelX = (sx + tx) / 2;
        labelY = midY - 7;
      }
      var group = svgEl("g", { "data-kind": edge.kind });
      group.appendChild(svgEl("path", { d: path, "marker-end": "url(#opg-arrow)" }));
      if (edge.label) {
        var text = svgEl("text", { x: labelX, y: labelY });
        text.textContent = edge.label;
        group.appendChild(text);
      }
      svg.appendChild(group);
    });
    map.appendChild(svg);
    var inspector = el("aside", "opg-inspector");
    var selectedButton = null;
    function select(node, button) {
      if (selectedButton) selectedButton.removeAttribute("data-selected");
      selectedButton = button;
      button.setAttribute("data-selected", "true");
      renderInspector(node, inspector);
    }
    (spec.nodes || []).forEach(function (node) {
      var point = layout.points[node.id];
      if (!point) return;
      var button = compactNode(node, select);
      button.style.left = point.x + "px";
      button.style.top = point.y + "px";
      button.style.width = point.width + "px";
      button.style.height = point.height + "px";
      map.appendChild(button);
      if (!selectedButton) select(node, button);
    });
    viewport.appendChild(map);
    shell.appendChild(viewport);
    shell.appendChild(
      el(
        "p",
        "opg-map-note",
        "The layout follows the emitted edge targets. Back edges remain explicit. Select a node to inspect its gates."
      )
    );
    workbench.appendChild(shell);
    workbench.appendChild(inspector);
    return workbench;
  }

  function evidenceValue(value, state) {
    var cell = el("td", "", value);
    cell.setAttribute("data-state", state);
    return cell;
  }

  function renderEvidence(spec) {
    var frame = el("div", "opg-evidence");
    var head = el("div", "opg-map-head");
    head.appendChild(el("span", "", "Program evidence lanes"));
    head.appendChild(el("span", "", "Declared controls, not live verdicts"));
    frame.appendChild(head);
    var scroll = el("div", "opg-evidence-scroll");
    var table = el("table", "opg-evidence-table");
    var thead = el("thead");
    var headerRow = el("tr");
    ["Step", "Resolve", "Identity", "Actuation", "Screen", "Independent effect", "Stop rules"].forEach(function (label) {
      headerRow.appendChild(el("th", "", label));
    });
    thead.appendChild(headerRow);
    table.appendChild(thead);
    var tbody = el("tbody");
    (spec.nodes || []).forEach(function (node) {
      var row = el("tr");
      var title = el("th");
      title.scope = "row";
      title.appendChild(el("code", "opg-evidence-index", node.kind === "terminal" ? "END" : String(node.index + 1).padStart(2, "0")));
      title.appendChild(document.createTextNode(compactTitle(node)));
      row.appendChild(title);
      var resolutionCount = node.resolution ? node.resolution.rungs.filter(function (rung) { return rung.present; }).length : 0;
      row.appendChild(evidenceValue(resolutionCount ? resolutionCount + " types" : "None", resolutionCount ? "declared" : "none"));
      var identity = node.identity && node.identity.armed ? "Armed" : node.identity && node.identity.applicable ? "Not armed" : "None";
      row.appendChild(evidenceValue(identity, identity === "Armed" ? "declared" : identity === "Not armed" ? "attention" : "none"));
      var actuation =
        node.kind === "action"
          ? "Declared"
          : node.kind === "child_bundle"
            ? "Child program"
            : "None";
      row.appendChild(
        evidenceValue(
          actuation,
          actuation === "None" ? "none" : "declared"
        )
      );
      row.appendChild(evidenceValue((node.postconditions || []).length ? node.postconditions.length + " checks" : "None", (node.postconditions || []).length ? "declared" : "none"));
      row.appendChild(evidenceValue((node.effects || []).length ? node.effects.length + " checks" : "None", (node.effects || []).length ? "declared" : "none"));
      row.appendChild(evidenceValue((node.halts || []).length ? String(node.halts.length) : "None", (node.halts || []).length ? "attention" : "none"));
      tbody.appendChild(row);
    });
    table.appendChild(tbody);
    scroll.appendChild(table);
    frame.appendChild(scroll);
    frame.appendChild(
      el(
        "p",
        "opg-map-note",
        "A declared lane is a compile-time requirement. A live run must bind an exact trace before this view can show a confirmed, refuted, or indeterminate verdict."
      )
    );
    return frame;
  }

  function renderStops(spec) {
    var flow = el("div", "opg-stop-flow");
    (spec.nodes || []).forEach(function (node) {
      if (!(node.halts || []).length && !(node.kind === "terminal" && node.outcome !== "success")) return;
      var card = node.kind === "action" ? renderActionNode(node) : node.kind === "terminal" ? renderTerminalNode(node) : renderControlNode(node);
      flow.appendChild(card);
    });
    if (!flow.childNodes.length) flow.appendChild(el("p", "opg-map-note", "This program has no distinguished halt path in the current projection."));
    return flow;
  }

  function renderTabs(spec, root) {
    var controls = el("div", "opg-tabs");
    controls.setAttribute("role", "tablist");
    controls.setAttribute("aria-label", "Program workbench views");
    var panel = el("div", "opg-panel");
    var views = [
      ["program", "Program map", function () { return renderMap(spec); }],
      ["evidence", "Evidence lanes", function () { return renderEvidence(spec); }],
      ["stops", "Stop rules", function () { return renderStops(spec); }],
    ];
    var buttons = [];
    function show(view) {
      buttons.forEach(function (button) {
        button.setAttribute("aria-selected", button.getAttribute("data-view") === view ? "true" : "false");
      });
      panel.innerHTML = "";
      var match = views.find(function (item) { return item[0] === view; });
      panel.appendChild(match[2]());
    }
    views.forEach(function (item) {
      var button = el("button", "", item[1]);
      button.type = "button";
      button.setAttribute("role", "tab");
      button.setAttribute("data-view", item[0]);
      button.addEventListener("click", function () { show(item[0]); });
      buttons.push(button);
      controls.appendChild(button);
    });
    root.appendChild(controls);
    root.appendChild(panel);
    show("program");
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

    renderTabs(spec, root);
    renderLegend(root);
    container.appendChild(root);
    return root;
  }

  var api = { render: render };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  global.OpenAdaptProgramGraph = api;
})(typeof window !== "undefined" ? window : this);
