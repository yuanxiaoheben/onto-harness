"use strict";

// Layout only declared hierarchical relations. Other relations may contain cycles.
function layoutOntology(ontology, schema) {
  const nodes = ontology.entities.map((item) => ({ ...item, depth: 0, x: 0, y: 0 }));
  const index = new Map(nodes.map((item) => [item.id, item]));
  const children = new Map(nodes.map((item) => [item.id, new Set()]));
  const degree = new Map(nodes.map((item) => [item.id, 0]));
  for (const edge of ontology.relations) {
    const spec = schema.relation_types?.[edge.type];
    if (!spec?.hierarchical || !index.has(edge.source) || !index.has(edge.target)) continue;
    const [parent, child] = spec.direction === "child_to_parent"
      ? [edge.target, edge.source] : [edge.source, edge.target];
    if (!children.get(parent).has(child)) {
      children.get(parent).add(child);
      degree.set(child, degree.get(child) + 1);
    }
  }
  const queue = nodes.filter((item) => degree.get(item.id) === 0);
  for (let i = 0; i < queue.length; i++) {
    const parent = queue[i];
    for (const id of children.get(parent.id)) {
      const child = index.get(id);
      child.depth = Math.max(child.depth, parent.depth + 1);
      degree.set(id, degree.get(id) - 1);
      if (!degree.get(id)) queue.push(child);
    }
  }
  const levels = new Map();
  for (const node of nodes) {
    if (!levels.has(node.depth)) levels.set(node.depth, []);
    levels.get(node.depth).push(node);
  }
  let nextY = 0;
  for (const [, items] of [...levels].sort((a, b) => a[0] - b[0])) {
    items.sort((a, b) => String(a.name).localeCompare(String(b.name)) || a.id.localeCompare(b.id));
    items.forEach((item, i) => {
      const row = Math.floor(i / 5), count = Math.min(5, items.length - row * 5);
      item.x = (i % 5 - (count - 1) / 2) * 205;
      item.y = nextY + row * 100;
    });
    nextY += Math.ceil(items.length / 5) * 100 + 35;
  }
  return nodes;
}

if (typeof module !== "undefined") module.exports = { layoutOntology };
