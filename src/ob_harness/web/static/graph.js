"use strict";

(() => {
  const tr = (key, params) => window.I18n.t(key, params);
  const svg = $("ontology-graph"), panel = $("graph-view"), detail = $("graph-detail");
  const source = $("graph-source"), filter = $("graph-relation"), status = $("graph-status"), legend = $("graph-legend");
  const ns = "http://www.w3.org/2000/svg";
  const NODE_PALETTE = [
    { stroke: "#6ea8fe", fill: "#13263c", muted: "#a7c8ff", glow: "rgba(110, 168, 254, 0.38)" },
    { stroke: "#5ad7c0", fill: "#102b2a", muted: "#a8eee1", glow: "rgba(90, 215, 192, 0.34)" },
    { stroke: "#f3c677", fill: "#2f2818", muted: "#f6dcaa", glow: "rgba(243, 198, 119, 0.34)" },
    { stroke: "#ff9bb5", fill: "#301a25", muted: "#ffc3d2", glow: "rgba(255, 155, 181, 0.34)" },
    { stroke: "#b59cff", fill: "#251d3a", muted: "#d3c6ff", glow: "rgba(181, 156, 255, 0.34)" },
    { stroke: "#ffb07a", fill: "#30211a", muted: "#ffd0b0", glow: "rgba(255, 176, 122, 0.34)" },
    { stroke: "#8fdb7a", fill: "#1b2b1b", muted: "#c5efb8", glow: "rgba(143, 219, 122, 0.32)" },
  ];
  let data = null, view = null, nodes = [], index = new Map(), nodeElements = new Map(), nodeColors = new Map(), edges = [];
  let layer, camera = { x: 0, y: 0, scale: 1 }, drag = null, request = 0, selected = null, dirty = true, lastError = null;

  function shape(tag, attrs = {}, text) {
    const item = document.createElementNS(ns, tag);
    for (const [key, value] of Object.entries(attrs)) item.setAttribute(key, value);
    if (text !== undefined) item.textContent = text;
    return item;
  }
  function label(node) {
    const aliases = node.aliases || [];
    const preferred = I18n.language === "en"
      ? aliases.find((name) => !/[\u3400-\u9fff]/.test(name))
      : aliases.find((name) => /[\u3400-\u9fff]/.test(name));
    return preferred || node.name || node.id;
  }
  function colorPlan() {
    const types = [...new Set(nodes.map((node) => String(node.type || "concept")))];
    const depths = [...new Set(nodes.map((node) => Number(node.depth) || 0))];
    const byType = types.length > 1 || depths.length === 1;
    const keyOf = byType
      ? (node) => String(node.type || "concept")
      : (node) => Number(node.depth) || 0;
    const keys = [...new Set(nodes.map(keyOf))].sort((a, b) =>
      typeof a === "number" ? a - b : a.localeCompare(b));
    const colors = new Map(keys.map((key, i) => [String(key), NODE_PALETTE[i % NODE_PALETTE.length]]));
    return { byType, keys, colors, keyOf };
  }
  function colorFor(plan, node) {
    return plan.colors.get(String(plan.keyOf(node))) || NODE_PALETTE[0];
  }
  function renderLegend(plan) {
    if (!legend) return;
    if (!nodes.length) { legend.hidden = true; legend.replaceChildren(); return; }
    const items = plan.keys.map((key) => {
      const color = plan.colors.get(String(key));
      const count = nodes.filter((node) => String(plan.keyOf(node)) === String(key)).length;
      const item = element("span", "legend-item");
      const dot = element("i", "legend-dot");
      dot.style.setProperty("--legend-color", color.stroke);
      dot.style.setProperty("--legend-glow", color.glow);
      const name = plan.byType ? key : key === 0 ? tr("graph.root") : tr("graph.level", { level: key });
      item.append(dot, `${name} · ${count}`);
      return item;
    });
    legend.replaceChildren(element("span", "legend-title", tr(plan.byType ? "graph.byType" : "graph.byDepth")), ...items);
    legend.hidden = false;
  }
  function transform() {
    if (layer) layer.setAttribute("transform", `translate(${camera.x} ${camera.y}) scale(${camera.scale})`);
  }
  function fit() {
    if (!nodes.length || panel.hidden) return;
    const left = Math.min(...nodes.map((n) => n.x)) - 110;
    const right = Math.max(...nodes.map((n) => n.x)) + 110;
    const top = Math.min(...nodes.map((n) => n.y)) - 55;
    const bottom = Math.max(...nodes.map((n) => n.y)) + 55;
    const width = svg.clientWidth, height = svg.clientHeight;
    const scale = Math.min(1.5, (width - 40) / (right - left), (height - 65) / (bottom - top));
    camera = { scale: Math.max(.05, scale), x: width / 2 - (left + right) / 2 * scale, y: height / 2 - (top + bottom) / 2 * scale };
    transform();
  }
  function zoom(factor, x = svg.clientWidth / 2, y = svg.clientHeight / 2) {
    const next = Math.max(.05, Math.min(4, camera.scale * factor)), ratio = next / camera.scale;
    camera = { x: x - (x - camera.x) * ratio, y: y - (y - camera.y) * ratio, scale: next };
    transform();
  }
  function edgePoints(a, b) {
    const dx = b.x - a.x, dy = b.y - a.y;
    const offset = Math.min(92 / (Math.abs(dx) || .001), 29 / (Math.abs(dy) || .001), .45);
    return [a.x + dx * offset, a.y + dy * offset, b.x - dx * offset, b.y - dy * offset];
  }
  function position() {
    for (const node of nodes) nodeElements.get(node.id).setAttribute("transform", `translate(${node.x} ${node.y})`);
    for (const { edge, path, text } of edges) {
      const a = index.get(edge.source), b = index.get(edge.target);
      const [x1, y1, x2, y2] = edgePoints(a, b);
      path.setAttribute("d", a.id === b.id ? `M ${a.x + 65} ${a.y - 29} C ${a.x + 160} ${a.y - 115}, ${a.x + 180} ${a.y + 70}, ${a.x + 92} ${a.y}` : `M ${x1} ${y1} L ${x2} ${y2}`);
      text.setAttribute("x", a.id === b.id ? a.x + 135 : (x1 + x2) / 2);
      text.setAttribute("y", a.id === b.id ? a.y - 50 : (y1 + y2) / 2 - 7);
    }
  }
  function renderDetailPlaceholder() {
    detail.replaceChildren(element("h2", "", tr("graph.detailTitle")), element("p", "", tr("graph.detailHint")));
  }
  function select(id) {
    const node = index.get(id);
    if (!node) return;
    selected = id;
    const related = data.ontology.relations.filter((r) => r.source === id || r.target === id);
    const neighbors = new Set([id, ...related.flatMap((r) => [r.source, r.target])]);
    for (const [key, group] of nodeElements) {
      group.classList.toggle("selected", key === id);
      group.classList.toggle("dimmed", !neighbors.has(key));
    }
    const pill = element("span", "type-pill", node.type || "concept");
    const color = nodeColors.get(id);
    if (color) {
      pill.style.setProperty("--pill-color", color.muted);
      pill.style.setProperty("--pill-bg", color.fill);
    }
    detail.replaceChildren(element("h2", "", label(node)), element("p", "graph-node-id", `${node.name} · ${node.id}`), pill);
    detail.append(element("p", "", node.definition || tr("graph.noDefinition")));
    if (node.aliases?.length) detail.append(element("p", "", tr("graph.aliases", { names: node.aliases.join("、") })));
    detail.append(element("h3", "", tr("graph.related", { count: related.length })));
    for (const edge of related) {
      const other = edge.source === id ? edge.target : edge.source;
      const button = element("button", "graph-neighbor", `${label(index.get(edge.source))} — ${edge.type} → ${label(index.get(edge.target))}`);
      button.type = "button";
      button.addEventListener("click", () => { select(other); center(other); });
      detail.append(button);
      if (edge.application_conditions) detail.append(element("p", "meta", tr("graph.conditions", { value: typeof edge.application_conditions === "string" ? edge.application_conditions : JSON.stringify(edge.application_conditions) })));
    }
    const ask = element("button", "review-confirm", tr("graph.ask"));
    ask.type = "button";
    ask.addEventListener("click", () => { show(false); els.input.value = `解释 ${node.name}`; autoGrow(); els.input.focus(); });
    detail.append(ask);
  }
  function center(id) {
    const node = index.get(id);
    camera.scale = Math.max(.8, camera.scale);
    camera.x = svg.clientWidth / 2 - node.x * camera.scale;
    camera.y = svg.clientHeight / 2 - node.y * camera.scale;
    transform();
  }
  function draw() {
    svg.replaceChildren(); edges = []; nodeElements = new Map(); nodeColors = new Map();
    const plan = colorPlan();
    const defs = shape("defs"), marker = shape("marker", { id: "graph-arrow", viewBox: "0 0 10 10", refX: 9, refY: 5, markerWidth: 7, markerHeight: 7, orient: "auto-start-reverse" });
    marker.append(shape("path", { d: "M 0 0 L 10 5 L 0 10 z", fill: "#7c9fc8" }));
    defs.append(marker); layer = shape("g"); svg.append(defs, layer);
    for (const edge of data.ontology.relations) {
      if ((filter.value && edge.type !== filter.value) || !index.has(edge.source) || !index.has(edge.target)) continue;
      const path = shape("path", { class: "graph-edge", "marker-end": "url(#graph-arrow)" });
      path.append(shape("title", {}, `${edge.source} — ${edge.type} → ${edge.target}`));
      const text = shape("text", { class: "graph-edge-label", "text-anchor": "middle" }, edge.type);
      layer.append(path, text); edges.push({ edge, path, text });
    }
    for (const node of nodes) {
      const color = colorFor(plan, node);
      const group = shape("g", { class: "graph-node", tabindex: 0, role: "button", "aria-label": tr("graph.nodeAria", { name: label(node) }) });
      group.dataset.nodeId = node.id;
      group.dataset.nodeType = node.type || "concept";
      group.style.setProperty("--node-color", color.stroke);
      group.style.setProperty("--node-fill", color.fill);
      group.style.setProperty("--node-muted", color.muted);
      group.style.setProperty("--node-glow", color.glow);
      group.append(shape("rect", { x: -92, y: -29, width: 184, height: 58, rx: 12 }));
      const name = label(node);
      const subtitle = node.name && node.name !== name ? node.name : node.type || "concept";
      group.append(shape("text", { "text-anchor": "middle", y: -3, class: "graph-node-label" }, name.length > 17 ? name.slice(0, 16) + "…" : name));
      group.append(shape("text", { "text-anchor": "middle", y: 16, class: "graph-node-subtitle" }, String(subtitle).slice(0, 25)));
      group.append(shape("title", {}, `${name}\n${node.definition || ""}`));
      group.addEventListener("click", () => select(node.id));
      group.addEventListener("keydown", (event) => { if (["Enter", " "].includes(event.key)) { event.preventDefault(); select(node.id); } });
      layer.append(group); nodeElements.set(node.id, group); nodeColors.set(node.id, color);
    }
    position(); transform();
    renderLegend(plan);
    if (selected && index.has(selected)) select(selected);
    else renderDetailPlaceholder();
  }
  function updateStatus() {
    if (!view) { status.textContent = tr("graph.loading"); return; }
    const draft = source.value === "draft" && Boolean(state.conversationId);
    const head = draft
      ? tr("graph.draftStatus", {
          revision: view.revision,
          status: tr(view.status === "committed" ? "graph.committedSnapshot" : "graph.uncommitted"),
        })
      : tr("graph.formalVersion", { version: shortVersion(view.version) });
    const counts = tr("graph.countSummary", {
      concepts: nodes.length,
      relations: view.ontology.relations.length,
    });
    const tail = nodes.length
      ? ` · ${tr(colorPlan().byType ? "graph.colorByType" : "graph.colorByDepth")} · ${tr("graph.arrowDirection")}`
      : ` · ${tr("graph.noConcepts")}`;
    status.textContent = `${head} · ${counts}${tail}`;
  }
  async function refresh() {
    const ticket = ++request;
    svg.replaceChildren(); renderDetailPlaceholder();
    if (legend) { legend.hidden = true; legend.replaceChildren(); }
    data = null; view = null; lastError = null; nodes = []; selected = null; layer = null; drag = null; nodeColors = new Map();
    status.textContent = tr("graph.loading");
    const draft = source.value === "draft" && state.conversationId;
    try {
      const response = await fetch(draft ? `/api/conversations/${encodeURIComponent(state.conversationId)}/draft` : "/api/ontology", { cache: "no-store" });
      const value = await response.json();
      if (ticket !== request) return;
      if (!response.ok) throw new Error(apiErrorMessage(value, `HTTP ${response.status}`));
      data = value; view = value;
      nodes = layoutOntology(value.ontology, value.schema); index = new Map(nodes.map((n) => [n.id, n]));
      const previous = filter.value;
      filter.replaceChildren(new Option(tr("graph.relationAll"), ""));
      [...new Set(value.ontology.relations.map((r) => r.type))].sort().forEach((type) => filter.add(new Option(type, type)));
      if ([...filter.options].some((o) => o.value === previous)) filter.value = previous;
      updateStatus();
      draw(); fit(); dirty = false;
    } catch (error) {
      if (ticket === request) {
        view = null;
        lastError = error;
        status.textContent = tr("graph.error", { message: error.message });
      }
    }
  }
  function show(graph) {
    panel.hidden = !graph; els.messages.hidden = graph; els.composer.hidden = graph;
    document.querySelector(".hint").hidden = graph;
    $("view-chat").setAttribute("aria-pressed", String(!graph));
    $("view-graph").setAttribute("aria-pressed", String(graph));
    $("view-title").textContent = tr(graph ? "view.titleGraph" : "view.titleChat");
    if (!graph) return;
    const previous = source.value;
    updateSource();
    if (previous !== source.value) dirty = true;
    if (dirty || !data) refresh();
    else { updateStatus(); draw(); fit(); }
  }
  function updateSource() {
    source.options[1].disabled = !state.conversationId;
    if (!state.conversationId) source.value = "formal";
  }
  svg.addEventListener("wheel", (event) => {
    event.preventDefault(); const box = svg.getBoundingClientRect();
    zoom(Math.exp(-event.deltaY * .0015), event.clientX - box.left, event.clientY - box.top);
  }, { passive: false });
  svg.addEventListener("pointerdown", (event) => {
    if (event.button !== 0 || !data) return;
    const id = event.target.closest("[data-node-id]")?.dataset.nodeId;
    drag = { id, x: event.clientX, y: event.clientY };
    svg.setPointerCapture(event.pointerId);
    if (id) select(id);
  });
  svg.addEventListener("pointermove", (event) => {
    if (!drag) return;
    const dx = event.clientX - drag.x, dy = event.clientY - drag.y;
    if (drag.id) { const node = index.get(drag.id); node.x += dx / camera.scale; node.y += dy / camera.scale; position(); }
    else { camera.x += dx; camera.y += dy; transform(); }
    drag.x = event.clientX; drag.y = event.clientY;
  });
  for (const name of ["pointerup", "pointercancel", "lostpointercapture"]) svg.addEventListener(name, () => { drag = null; });
  $("view-chat").addEventListener("click", () => show(false));
  $("view-graph").addEventListener("click", () => show(true));
  $("graph-refresh").addEventListener("click", refresh);
  $("graph-fit").addEventListener("click", fit);
  $("graph-zoom-in").addEventListener("click", () => zoom(1.25));
  $("graph-zoom-out").addEventListener("click", () => zoom(.8));
  source.addEventListener("change", () => { dirty = true; refresh(); });
  filter.addEventListener("change", () => { if (data) draw(); });
  function find() {
    const query = $("graph-search").value.trim().toLocaleLowerCase();
    if (!query) return;
    const matches = nodes.filter((n) => [n.name, n.id, ...(n.aliases || [])].some((s) => String(s).toLocaleLowerCase().includes(query)));
    if (matches.length) { select(matches[0].id); center(matches[0].id); }
    else detail.replaceChildren(element("p", "", tr("graph.noMatch")));
  }
  $("graph-find").addEventListener("click", find);
  $("graph-search").addEventListener("keydown", (event) => { if (event.key === "Enter") find(); });
  document.addEventListener("ontology:changed", () => { dirty = true; updateSource(); if (!panel.hidden) refresh(); });
  document.addEventListener("language:changed", () => {
    $("view-title").textContent = tr(panel.hidden ? "view.titleChat" : "view.titleGraph");
    if (filter.options[0]) filter.options[0].textContent = tr("graph.relationAll");
    if (view) {
      updateStatus();
      if (data) { draw(); fit(); }
    } else if (lastError) {
      status.textContent = tr("graph.error", { message: lastError.message });
    } else {
      status.textContent = tr("graph.loading");
    }
  });
  new ResizeObserver(() => { if (!panel.hidden) fit(); }).observe($("graph-canvas"));
})();
