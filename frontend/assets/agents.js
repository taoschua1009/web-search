"use strict";
const $ = (id) => document.getElementById(id);
const state = { snapshot: null, receivedAt: 0, source: null };

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === "class") node.className = value;
    else if (key === "style") node.setAttribute("style", value);
    else node.setAttribute(key, String(value));
  }
  for (const child of children.flat(Infinity))
    if (child !== null && child !== undefined)
      node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  return node;
}

const roleLabels = {
  chat: "CHAT", email: "EMAIL", search: "TÌM KIẾM", specialist: "CHUYÊN GIA",
  memory: "BỘ NHỚ", calendar: "LỊCH", report: "BÁO CÁO", agent: "AGENT",
};
const kindLabels = { chat: "CHAT", research: "NGHIÊN CỨU" };
const showFinished = { value: false };
try { showFinished.value = localStorage.getItem("agents.showFinished") === "1"; } catch { /* storage unavailable */ }

function fmtDuration(ms) {
  if (ms < 1000) return Math.round(ms) + " ms";
  const seconds = ms / 1000;
  if (seconds < 60) return seconds.toFixed(1) + " s";
  return Math.floor(seconds / 60) + "m " + String(Math.round(seconds % 60)).padStart(2, "0") + "s";
}
function fmtCost(usd) {
  if (usd === 0) return "$0";
  return usd < 0.01 ? "$" + usd.toFixed(5) : "$" + usd.toFixed(4);
}
function fmtTokens(n) {
  return n >= 10000 ? (n / 1000).toFixed(1) + "k" : String(n);
}

/** Elapsed time of a running item advances locally between server frames. */
function liveTime(baseMs, running) {
  const node = el("b", { class: "live-time" }, fmtDuration(baseMs));
  node.dataset.base = String(baseMs);
  node.dataset.running = running ? "1" : "0";
  return node;
}
function metric(label, value) {
  return el("span", {}, label + " ", value instanceof Node ? value : el("b", {}, value));
}

function dot(status) {
  return el("span", { class: "n-dot" + (status === "running" ? "" : status === "completed" ? " done" : " bad") });
}

function toolNode(tool) {
  return el(
    "li",
    { class: "tool-node" },
    el(
      "div",
      { class: "n-row" },
      el("span", { class: "tool" }, tool.name),
      el("span", { class: "detail" }, tool.detail || ""),
      el("span", { class: "n-metrics" }, liveTime(tool.elapsed_ms, true)),
    ),
  );
}

function agentNode(agent) {
  const llm = agent.llm;
  const running = agent.status === "running";
  const metrics = el("span", { class: "n-metrics" }, metric("⏱", liveTime(agent.elapsed_ms, running)));
  if (llm.calls || agent.role === "specialist") {
    metrics.append(
      metric("gọi mô hình", String(llm.calls)),
      metric("token", `${fmtTokens(llm.input_tokens)}/${fmtTokens(llm.output_tokens)}`),
      metric("chi phí", fmtCost(llm.cost_usd)),
      metric("độ trễ TB", llm.calls ? fmtDuration(llm.avg_ms) : "—"),
    );
  } else {
    const calls = Object.values(agent.tool_counts).reduce((n, t) => n + t.calls, 0);
    if (calls) metrics.append(metric("công cụ đã gọi", String(calls)));
  }
  const children = agent.active_tools.map(toolNode);
  if (running && !children.length && agent.role === "specialist")
    children.push(el("li", { class: "tool-node" }, el("div", { class: "n-row thinking" }, "đang chờ mô hình trả lời…")));
  return el(
    "li",
    { class: "agent" },
    el(
      "div",
      { class: "n-row" },
      dot(agent.status),
      el("span", { class: "n-name" }, agent.name),
      el("span", { class: "n-chip" + (running ? " mint" : "") }, roleLabels[agent.role] || agent.role.toUpperCase()),
      agent.model ? el("span", { class: "n-chip model" }, agent.model) : null,
      metrics,
    ),
    children.length ? el("ul", {}, children) : null,
  );
}

function visibleAgents(team) {
  return showFinished.value ? team.agents : team.agents.filter((agent) => agent.status === "running");
}

function groupNode(name, agents, allAgents) {
  const running = agents.filter((agent) => agent.status === "running").length;
  const cost = allAgents.reduce((sum, agent) => sum + agent.llm.cost_usd, 0);
  const done = allAgents.length - allAgents.filter((agent) => agent.status === "running").length;
  return el(
    "li",
    { class: "group" },
    el(
      "div",
      { class: "n-row" },
      el("span", { class: "group-label" }, name),
      el("span", { class: "n-chip mint" }, `${running} đang chạy`),
      done ? el("span", { class: "n-chip" }, `${done} đã xong`) : null,
      el("span", { class: "n-metrics" }, metric("chi phí", fmtCost(cost))),
    ),
    el("ul", {}, agents.map(agentNode)),
  );
}

function teamNode(team, byParent) {
  const running = team.status === "running";
  const shown = visibleAgents(team);
  const direct = shown.filter((agent) => !agent.group);
  const groups = new Map();
  for (const agent of shown) {
    if (!agent.group) continue;
    if (!groups.has(agent.group)) groups.set(agent.group, []);
    groups.get(agent.group).push(agent);
  }
  const children = [
    ...direct.map(agentNode),
    ...[...groups].map(([name, agents]) => groupNode(name, agents, team.agents.filter((a) => a.group === name))),
    ...(byParent.get(team.id) || []).map((child) => teamNode(child, byParent)),
  ];
  if (running && !children.length)
    children.push(el("li", { class: "tool-node" }, el("div", { class: "n-row thinking" }, "đang chuyển giai đoạn…")));
  const live = team.agents.filter((agent) => agent.status === "running").length;
  return el(
    "li",
    { class: "team" },
    el(
      "div",
      { class: "n-row" },
      dot(team.status),
      el("span", { class: "n-chip" }, kindLabels[team.kind] || team.kind.toUpperCase()),
      el("span", { class: "n-name" }, team.title),
      el(
        "span",
        { class: "n-metrics" },
        metric("agent", `${live} đang chạy / ${team.agents.length}`),
        metric("chi phí", fmtCost(team.cost_usd)),
        metric("⏱", liveTime(team.elapsed_ms, running)),
      ),
    ),
    el("ul", {}, children),
  );
}

function kpi(value, label, extra = "") {
  return el("div", { class: "a-kpi " + extra }, el("strong", {}, value), el("span", {}, label));
}

function render() {
  const snapshot = state.snapshot;
  if (!snapshot) return;
  const t = snapshot.totals;
  $("kpis").replaceChildren(
    kpi(`${t.agents_running}`, "agent đang chạy", t.agents_running ? "running" : ""),
    kpi(`${t.teams_running}`, "đội đang chạy", t.teams_running ? "running" : ""),
    kpi(String(t.llm_calls), "lượt gọi mô hình"),
    kpi(t.llm_calls ? fmtDuration(t.avg_llm_ms) : "—", "độ trễ mô hình trung bình"),
    kpi(String(t.tool_calls), "lần gọi công cụ"),
    kpi(fmtCost(t.cost_usd), "tổng chi phí ước tính"),
  );
  const teams = snapshot.teams.filter((team) => showFinished.value || team.status === "running");
  const ids = new Set(teams.map((team) => team.id));
  const byParent = new Map();
  const roots = [];
  for (const team of [...teams].reverse()) {
    if (team.parent && ids.has(team.parent)) {
      if (!byParent.has(team.parent)) byParent.set(team.parent, []);
      byParent.get(team.parent).push(team);
    } else roots.unshift(team);
  }
  const hidden = snapshot.teams.length - teams.length;
  $("teams").replaceChildren(
    roots.length
      ? el("div", { class: "tree-wrap" }, el("ul", { class: "tree" }, roots.map((team) => teamNode(team, byParent))))
      : el(
          "div",
          { class: "a-empty" },
          el("h3", {}, "Không có agent nào đang chạy"),
          el(
            "p",
            {},
            "Gửi một yêu cầu trong Chat hoặc bắt đầu nghiên cứu email; cây agent sẽ hiện ra ngay khi chúng chạy." +
              (hidden ? ` (${hidden} đội đã xong — bật “Hiện cả agent đã xong” để xem.)` : ""),
          ),
        ),
  );
  tick();
}

/** Advance the clocks of running items between server frames. */
function tick() {
  const elapsed = Date.now() - state.receivedAt;
  for (const node of document.querySelectorAll(".live-time")) {
    if (node.dataset.running === "1") node.textContent = fmtDuration(Number(node.dataset.base) + elapsed);
  }
}

function setLive(connected) {
  $("liveDot").className = "live-dot" + (connected ? "" : " off");
  $("liveText").textContent = connected ? "Trực tiếp" : "Mất kết nối — đang thử lại…";
}

function connect() {
  state.source?.close();
  const source = new EventSource("/api/activity/stream");
  state.source = source;
  source.onopen = () => setLive(true);
  source.onmessage = (event) => {
    state.snapshot = JSON.parse(event.data);
    state.receivedAt = Date.now();
    setLive(true);
    render();
  };
  source.onerror = () => {
    setLive(false);
    // EventSource retries by itself; after a hard close, reopen explicitly.
    if (source.readyState === EventSource.CLOSED) setTimeout(connect, 3000);
  };
}

$("showFinished").checked = showFinished.value;
$("showFinished").addEventListener("change", (event) => {
  showFinished.value = event.target.checked;
  try { localStorage.setItem("agents.showFinished", showFinished.value ? "1" : "0"); } catch { /* ignore */ }
  render();
});
$("clearBtn").addEventListener("click", async () => {
  await fetch("/api/activity/clear", { method: "POST", headers: { "x-research-request": "1" } });
});
fetch("/api/me").then((r) => r.json()).then((u) => { $("accountEmail").textContent = u.email; }).catch(() => {});
setInterval(tick, 250);
connect();
