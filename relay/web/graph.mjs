// Layout and hit testing are independent of the simulator and network.
export const branchName = (branch) => branch.parent ? `分岐 ${Number(branch.id)}` : "元の経路";
export function routeSegments(branches, branchId, end) {
  const branch = branches.find((item) => item.id === branchId);
  if (!branch) return [];
  if (!branch.parent) return [{ branch: branch.id, start: 0, end }];
  const cut = Math.min(end, branch.parent_step);
  const prefix = routeSegments(branches, branch.parent, cut);
  return end <= branch.parent_step ? prefix : [...prefix, { branch: branch.id, start: branch.parent_step, end }];
}
export function visiblePoint(branches, branchId, step) {
  const branch = branches.find((item) => item.id === branchId);
  if (branch?.parent && step <= branch.parent_step)
    return visiblePoint(branches, branch.parent, step);
  return { branch: branchId, step };
}
export function graphLayout(history, width = 740) {
  const left = 102, right = 100, top = 44, rowHeight = 38;
  const maximum = Math.max(1, ...history.branches.map((branch) => branch.head));
  width = Math.max(570, width);
  const x = (step) => left + step / maximum * (width - left - right);
  const stride = Math.max(1, Math.ceil(maximum / ((width - left - right) / 24)));
  const rows = history.branches.map((branch, index) => {
    const start = branch.parent ? branch.parent_step : 0;
    const steps = new Set([start, branch.head]);
    for (let step = Math.ceil(start / stride) * stride; step < branch.head; step += stride) steps.add(step);
    for (const child of history.branches) {
      if (!child.parent) continue;
      const origin = visiblePoint(history.branches, child.parent, child.parent_step);
      if (origin.branch === branch.id) steps.add(origin.step);
    }
    for (const point of [visiblePoint(history.branches, history.branch, history.cursor),
      history.adopted && visiblePoint(history.branches, history.adopted.branch, history.adopted.end_step)])
      if (point?.branch === branch.id) steps.add(point.step);
    return { ...branch, start, y: top + index * rowHeight,
      nodes: [...steps].sort((a, b) => a - b).map((step) => ({ step, x: x(step) })) };
  });
  return { width, height: top + rows.length * rowHeight + 14, left, right, maximum, rows, x };
}
export function stepAtX(layout, branchId, x) {
  const branch = layout.rows.find((item) => item.id === branchId);
  if (!branch) return null;
  const step = Math.round((x - layout.left) / (layout.width - layout.left - layout.right) * layout.maximum);
  return Math.max(branch.start, Math.min(branch.head, step));
}
const NS = "http://www.w3.org/2000/svg";
function svgNode(name, attrs = {}, text) {
  const element = document.createElementNS(NS, name);
  for (const [key, value] of Object.entries(attrs)) element.setAttribute(key, value);
  if (text !== undefined) element.textContent = text;
  return element;
}
const sourceClass = (source) => source?.includes("VLA") ? "vla" : ["HUMAN", "TEST_OPERATOR"].includes(source)
  ? "human" : source === "CODE" ? "code" : "replay";
export class BranchGraph {
  constructor(container, onSeek, onHover) {
    this.container = container;
    this.onSeek = onSeek;
    this.onHover = onHover;
    this.key = null;
    container.addEventListener("click", (event) => this.activate(event));
    container.addEventListener("keydown", (event) => {
      if (["Enter", " "].includes(event.key) && event.target.closest("[data-branch]")) {
        event.preventDefault();
        event.stopPropagation();
        this.activate(event);
      }
    });
    container.addEventListener("pointermove", (event) => {
      const target = event.target.closest("[data-branch]");
      if (!target || !this.layout) return;
      const bounds = this.svg.getBoundingClientRect();
      const step = target.dataset.step === undefined
        ? stepAtX(this.layout, target.dataset.branch, (event.clientX - bounds.left) / bounds.width * this.layout.width)
        : Number(target.dataset.step);
      this.onHover(`${branchName(this.layout.rows.find((row) => row.id === target.dataset.branch))} · step ${step}へ戻る`);
    });
    container.addEventListener("pointerleave", () => this.onHover("点・線をクリックして、そのstepへ戻れます"));
    this.observer = new ResizeObserver(() => { this.key = null; if (this.history) this.render(this.history, this.busy); });
    this.observer.observe(container);
  }
  activate(event) {
    if (this.busy) return;
    const target = event.target.closest("[data-branch]");
    if (!target || !this.layout) return;
    const bounds = this.svg.getBoundingClientRect();
    const step = target.dataset.step === undefined
      ? stepAtX(this.layout, target.dataset.branch, (event.clientX - bounds.left) / bounds.width * this.layout.width)
      : Number(target.dataset.step);
    if (step !== null) this.onSeek(step, target.dataset.branch);
  }
  render(history, busy) {
    this.history = history;
    this.busy = busy;
    this.container.classList.toggle("busy", busy);
    this.container.setAttribute("aria-busy", String(busy));
    const key = JSON.stringify([history, Math.round(this.container.clientWidth)]);
    if (key === this.key) return;
    this.key = key;
    const layout = this.layout = graphLayout(history, this.container.clientWidth);
    const svg = this.svg = svgNode("svg", { viewBox: `0 0 ${layout.width} ${layout.height}`, width: layout.width,
      height: layout.height, role: "group", "aria-label": "分岐図。点をクリックするとそのstepへ戻ります" });
    const headPoint = visiblePoint(history.branches, history.branch, history.cursor);
    const adopted = history.adopted ? routeSegments(history.branches, history.adopted.branch, history.adopted.end_step) : [];
    for (let index = 0; index <= 4; index++) {
      const step = Math.round(layout.maximum * index / 4), x = layout.x(step);
      svg.append(svgNode("line", { x1: x, x2: x, y1: 23, y2: layout.height - 12, class: "graph-grid" }),
        svgNode("text", { x, y: 13, "text-anchor": "middle", class: "graph-tick" }, String(step)));
    }
    svg.append(svgNode("text", { x: 12, y: 13, class: "graph-tick" }, "STEP"));
    for (const row of layout.rows) {
      if (history.branch === row.id)
        svg.append(svgNode("rect", { x: 2, y: row.y - 16, width: layout.width - 4, height: 32, rx: 7, class: "graph-active-row" }));
      const selected = adopted.find((segment) => segment.branch === row.id);
      if (row.parent) {
        const origin = visiblePoint(history.branches, row.parent, row.parent_step);
        const parent = layout.rows.find((item) => item.id === origin.branch);
        const x = layout.x(row.parent_step), next = layout.x(Math.min(row.head, row.parent_step + 3));
        svg.append(svgNode("path", { d: `M ${x} ${parent.y} C ${x} ${row.y} ${next} ${row.y} ${next} ${row.y}`,
          class: "graph-connector" + (selected ? " adopted" : "") }));
      }
      if (selected)
        svg.append(svgNode("line", { x1: layout.x(selected.start), x2: layout.x(selected.end), y1: row.y, y2: row.y, class: "graph-adopted-route" }));
      svg.append(svgNode("line", { x1: layout.x(row.start), x2: layout.x(row.head), y1: row.y, y2: row.y, class: "graph-track" }));
      for (const segment of row.segments || [])
        svg.append(svgNode("line", { x1: layout.x(segment.start), x2: layout.x(segment.end), y1: row.y, y2: row.y,
          class: "graph-source " + sourceClass(segment.source) }));
      // A wide hit target lets every step be chosen even when overview dots are sampled.
      svg.append(svgNode("rect", { x: layout.x(row.start) - 6, y: row.y - 13,
        width: Math.max(12, layout.x(row.head) - layout.x(row.start) + 12), height: 26,
        fill: "transparent", "data-branch": row.id, class: "graph-hit" }));
      const label = svgNode("foreignObject", {x: 8, y: row.y-13, width: 88, height: 26});
      const labelButton = document.createElement("button");
      labelButton.className = "graph-label-button";
      labelButton.dataset.branch = row.id;
      labelButton.dataset.step = row.head;
      labelButton.setAttribute("aria-label", `${branchName(row)}の末尾 step ${row.head}へ戻る`);
      labelButton.textContent = branchName(row);
      label.append(labelButton);
      svg.append(label);
      for (const node of row.nodes) {
        const group = svgNode("g", { "data-branch": row.id, "data-step": node.step, class: "graph-node" + (node.step === row.head ? " is-end" : "") +
            (headPoint.branch === row.id && headPoint.step === node.step ? " is-current" : ""),
          "aria-label": `${branchName(row)} step ${node.step}へ戻る` });
        group.append(svgNode("circle", { cx: node.x, cy: row.y, r: node.step === row.head || (headPoint.branch === row.id && headPoint.step === node.step) ? 5 : 2.5 }),
          svgNode("title", {}, `${branchName(row)} · step ${node.step}`));
        const hit = svgNode("foreignObject", {x: node.x-8, y: row.y-11, width: 16, height: 22});
        const button = document.createElement("button");
        button.className = "graph-node-button";
        button.dataset.branch = row.id;
        button.dataset.step = node.step;
        button.tabIndex = node.step === row.head ? 0 : -1;
        button.setAttribute("aria-label", `${branchName(row)} step ${node.step}へ戻る`);
        button.title = `${branchName(row)} · step ${node.step}`;
        hit.append(button);
        group.append(hit);
        svg.append(group);
      }
      svg.append(svgNode("text", { x: layout.x(row.head) + 11, y: row.y + 4, class: "graph-head-label",
        "data-branch": row.id, "data-step": row.head }, String(row.head)));
    }
    if (history.adopted) {
      const point = visiblePoint(history.branches, history.adopted.branch, history.adopted.end_step);
      const row = layout.rows.find((item) => item.id === point.branch);
      svg.append(svgNode("circle", { cx: layout.x(point.step), cy: row.y, r: 8, class: "graph-adopted-end", "pointer-events": "none" }));
    }
    const head = visiblePoint(history.branches, history.branch, history.cursor);
    const row = layout.rows.find((item) => item.id === head.branch), x = layout.x(head.step);
    svg.append(svgNode("path", { d: `M ${x} ${row.y - 10} l -5 -7 h 10 Z`, class: "graph-head-marker", "pointer-events": "none" }),
      svgNode("text", { x, y: row.y - 20, "text-anchor": "middle", class: "graph-head-text", "pointer-events": "none" }, "HEAD"));
    const previousScroll = [this.container.scrollLeft, this.container.scrollTop];
    this.container.replaceChildren(svg);
    [this.container.scrollLeft, this.container.scrollTop] = previousScroll;
  }
}
