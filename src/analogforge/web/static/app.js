/* AnalogForge single-page UI (vanilla JS + Plotly served locally). */
"use strict";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => Array.from(el.querySelectorAll(s));
const state = { info: null, design: null, compare: new Set(), jobTimer: null, currentJob: null };

function esc(s) { return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }

async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  const text = await res.text();
  let data; try { data = JSON.parse(text); } catch { data = text; }
  if (!res.ok) throw new Error((data && data.detail) || text || res.statusText);
  return data;
}

const PREFIX = [[1e12, "T"], [1e9, "G"], [1e6, "M"], [1e3, "k"], [1, ""], [1e-3, "m"], [1e-6, "µ"], [1e-9, "n"], [1e-12, "p"], [1e-15, "f"]];
function fmt(v, unit = "") {
  if (v === null || v === undefined || (typeof v === "number" && !isFinite(v))) return "n/a";
  if (typeof v !== "number") return esc(v);
  if (["dB", "dBc/Hz", "deg", "%", "", "V/V"].includes(unit)) return `${+v.toPrecision(5)} ${unit}`.trim();
  if (v === 0) return `0 ${unit}`;
  const a = Math.abs(v);
  for (const [s, p] of PREFIX) if (a >= s * 0.9995) return `${+(v / s).toPrecision(4)} ${p}${unit}`;
  return `${v.toExponential(3)} ${unit}`;
}
function unitOf(name) { const m = (state.design?.metrics || []).find(x => x.name === name); return m ? m.unit : ""; }

/* ---------------------------------------------------------------- tabs */
function showTab(name) {
  $$("#tabs button").forEach(b => b.classList.toggle("active", b.dataset.tab === name));
  $$(".tab").forEach(t => t.classList.toggle("active", t.id === `tab-${name}`));
  if (name === "jobs") loadJobs();
  if (name === "compare") loadCompare();
  if (name === "system") loadSystem();
}

/* ---------------------------------------------------------------- plotting */
function plotCurve(el, curve, extraTraces = null) {
  if (typeof Plotly === "undefined") { el.innerHTML = '<p class="muted">Plotly not available (pip install plotly).</p>'; return; }
  const traces = extraTraces || Object.entries(curve.series).map(([name, ys]) => ({ x: curve.x, y: ys, name, mode: "lines" }));
  const dark = matchMedia("(prefers-color-scheme: dark)").matches;
  Plotly.newPlot(el, traces, {
    title: { text: curve.title || curve.name, font: { size: 13 } }, margin: { t: 36, r: 12, b: 44, l: 60 },
    xaxis: { title: curve.xlabel, type: curve.xlog ? "log" : "linear", gridcolor: dark ? "#333" : "#eee" },
    yaxis: { title: curve.ylabel, type: curve.ylog ? "log" : "linear", gridcolor: dark ? "#333" : "#eee" },
    paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "rgba(0,0,0,0)", font: { color: dark ? "#ddd" : "#222" },
    legend: { orientation: "h", y: -0.25 }, showlegend: traces.length > 1,
  }, { responsive: true, displaylogo: false });
}

/* ---------------------------------------------------------------- new job form */
async function loadDesign(name) {
  const d = await api(`/api/designs/${encodeURIComponent(name)}`);
  state.design = d;
  $("#f-design-desc").textContent = (d.description || "").split("\n").slice(0, 6).join("\n");
  $("#f-options").value = JSON.stringify(d.options || {}, null, 1);
  const rows = d.parameters.map(p => `<tr data-name="${esc(p.name)}">
      <td><b>${esc(p.name)}</b></td>
      <td><input class="p-val" value="${p.default}"></td><td>${esc(p.unit)}</td>
      <td><input class="p-sweep" placeholder="start:stop:num"></td>
      <td><input type="checkbox" class="p-opt" ${p.tunable && p.min !== null ? "" : "disabled"}>
          <input class="p-min" value="${p.min ?? ""}" style="width:80px;display:inline-block">
          <input class="p-max" value="${p.max ?? ""}" style="width:80px;display:inline-block"></td>
      <td class="muted small">${esc(p.description)}</td></tr>`).join("");
  $("#param-table").innerHTML = `<tr><th>Parameter</th><th>Value</th><th>Unit</th><th>Sweep</th><th>Opt (min / max)</th><th>Description</th></tr>${rows}`;
  $("#op-metric").innerHTML = d.metrics.map(m => `<option value="${esc(m.name)}" data-goal="${m.goal}">${esc(m.name)} ${m.unit ? "[" + esc(m.unit) + "]" : ""}</option>`).join("");
  if (d.default_objective) {
    const [g, m] = d.default_objective.split(":"); $("#op-goal").value = g; $("#op-metric").value = m;
  }
  $("#op-cons").value = (d.default_constraints || []).join("\n");
}

function fillPdk(selectEl, cornerEl) {
  const pdk = state.info.pdks.find(p => p.name === selectEl.value) || state.info.pdks[0];
  if (cornerEl) cornerEl.innerHTML = (pdk?.corners || []).map(c => `<option>${esc(c)}</option>`).join("");
  $("#pvt-process").innerHTML = (pdk?.corners || []).map(c => `<label><input type="checkbox" value="${esc(c)}" checked>${esc(c)}</label>`).join("");
  if (pdk?.temperatures) $("#pvt-temp").value = pdk.temperatures.join(",");
}

function updateKindPanels() {
  const k = $("#f-kind").value;
  $$("#kind-panels > div[data-kind]").forEach(el => el.classList.toggle("hidden", el.dataset.kind !== k));
}

function parseList(s, num = true) { return s.split(",").map(x => x.trim()).filter(Boolean).map(x => num ? Number(x) : x); }
function sweepSpec(s) {
  if (s.includes(":")) { const p = s.split(":"); const o = { start: p[0], stop: p[1], num: Number(p[2] || 5) }; if (p[3] === "log") o.log = true; return o; }
  return s.split(",").map(x => x.trim()).filter(Boolean);
}

async function submitJob() {
  const msg = $("#submit-msg"); msg.textContent = "";
  try {
    const kind = $("#f-kind").value;
    const params = {}; const sweepVars = {}; const optVars = {};
    $$("#param-table tr[data-name]").forEach(tr => {
      const n = tr.dataset.name, p = state.design.parameters.find(x => x.name === n);
      const v = $(".p-val", tr).value.trim();
      if (v !== "" && String(p.default) !== v) params[n] = v;
      const sw = $(".p-sweep", tr).value.trim(); if (sw) sweepVars[n] = sweepSpec(sw);
      if ($(".p-opt", tr).checked) optVars[n] = [$(".p-min", tr).value, $(".p-max", tr).value];
    });
    let options = {};
    const ot = $("#f-options").value.trim(); if (ot) options = JSON.parse(ot);
    const cfg = {
      kind, design: state.design.name, name: $("#f-name").value, backend: $("#f-backend").value,
      pdk: $("#f-pdk").value, corner: $("#f-corner").value || null, temperature: Number($("#f-temp").value),
      vdd_scale: Number($("#f-vdd").value), params, options,
    };
    const w = $("#f-workers").value; if (w) cfg.workers = Number(w);
    if (kind === "sweep") {
      if ($("#sw-corner").value) sweepVars.corner = parseList($("#sw-corner").value, false);
      if ($("#sw-temp").value) sweepVars.temperature = parseList($("#sw-temp").value);
      if ($("#sw-vdd").value) sweepVars.vdd_scale = parseList($("#sw-vdd").value);
      if (!Object.keys(sweepVars).length) throw new Error("enter a sweep spec for at least one parameter");
      cfg.sweep = { variables: sweepVars, mode: $("#sw-mode").value };
    } else if (kind === "corners") {
      cfg.corners = { process: $$("#pvt-process input:checked").map(i => i.value),
        voltage: parseList($("#pvt-voltage").value), temperature: parseList($("#pvt-temp").value) };
    } else if (kind === "optimize") {
      cfg.optimize = { method: $("#op-method").value, objective: `${$("#op-goal").value}:${$("#op-metric").value}`,
        constraints: $("#op-cons").value.split("\n").map(s => s.trim()).filter(Boolean),
        budget: Number($("#op-budget").value), seed: Number($("#op-seed").value),
        variables: Object.keys(optVars).length ? optVars : null };
    }
    const r = await api("/api/jobs", { method: "POST", body: JSON.stringify(cfg) });
    msg.innerHTML = `<span class="ok">submitted ${esc(r.job_id)}</span>`;
    state.currentJob = r.job_id;
    showTab("jobs"); openJob(r.job_id);
  } catch (e) { msg.innerHTML = `<span class="bad">${esc(e.message)}</span>`; }
}

/* ---------------------------------------------------------------- jobs */
function statusCell(j) {
  const pct = Math.round(100 * (j.progress || 0));
  return `<span class="status-${j.status}">${j.status}</span>
    <span class="progress"><div style="width:${pct}%"></div></span> <span class="small muted">${j.done || 0}/${j.total || 0}</span>`;
}

async function loadJobs() {
  const jobs = await api("/api/jobs");
  $("#jobs-table").innerHTML = `<tr><th>Job</th><th>Name</th><th>Kind</th><th>Design</th><th>Backend</th><th>Status</th><th>Created</th><th></th></tr>` +
    jobs.map(j => `<tr><td><a href="#" data-job="${j.id}">${j.id}</a></td><td>${esc(j.name)}</td><td>${j.kind}</td>
      <td>${esc(j.config?.design)}</td><td>${esc(j.config?.backend)}</td><td>${statusCell(j)}</td>
      <td class="small">${new Date(j.created * 1000).toLocaleString()}</td>
      <td>${["queued", "running"].includes(j.status) ? `<button data-cancel="${j.id}">cancel</button>` : `<button data-del="${j.id}">delete</button>`}</td></tr>`).join("");
  $$("#jobs-table a[data-job]").forEach(a => a.onclick = ev => { ev.preventDefault(); openJob(a.dataset.job); });
  $$("#jobs-table button[data-cancel]").forEach(b => b.onclick = async () => { await api(`/api/jobs/${b.dataset.cancel}/cancel`, { method: "POST" }); loadJobs(); });
  $$("#jobs-table button[data-del]").forEach(b => b.onclick = async () => { await api(`/api/jobs/${b.dataset.del}`, { method: "DELETE" }); if (state.currentJob === b.dataset.del) $("#job-detail").classList.add("hidden"); loadJobs(); });
  const running = jobs.some(j => ["queued", "running"].includes(j.status));
  clearTimeout(state.jobTimer);
  if (running && $("#tab-jobs").classList.contains("active")) state.jobTimer = setTimeout(() => { loadJobs(); if (state.currentJob) openJob(state.currentJob, true); }, 2000);
}

function metricColumns(runs, limit = 8) {
  const names = [];
  for (const r of runs) for (const [k, v] of Object.entries(r.metrics || {})) if (v !== null && !names.includes(k)) names.push(k);
  return names.slice(0, limit);
}

async function openJob(id, quiet = false) {
  state.currentJob = id;
  const j = await api(`/api/jobs/${id}`);
  const el = $("#job-detail"); el.classList.remove("hidden");
  if (!state.design || state.design.name !== j.config.design) { try { state.design = await api(`/api/designs/${encodeURIComponent(j.config.design)}`); } catch { } }
  const s = j.summary || {};
  const labels = j.runs.length ? Object.keys(j.runs[0].label || {}) : [];
  const mcols = metricColumns(j.runs);
  let html = `<h2>Job ${esc(j.id)} &mdash; ${esc(j.kind)} of <code>${esc(j.config.design)}</code> ${statusCell(j)}</h2>`;
  if (j.error) html += `<p class="bad">${esc(j.error)}</p>`;
  html += `<p class="small muted">backend ${esc(j.config.backend)}, PDK ${esc(j.config.pdk)}, elapsed ${s.elapsed_s ?? "…"} s
     &nbsp; <a href="/api/report/${j.id}" target="_blank">Markdown report</a></p>`;
  if (j.kind === "optimize" && s.best) {
    html += `<h3>Best point (${s.best.feasible ? '<span class="ok">feasible</span>' : '<span class="warn">infeasible</span>'}, ${esc(s.method)}, ${s.evaluations} evals)</h3>
      <table class="data"><tr>${Object.keys(s.best.params).map(k => `<th>${esc(k)}</th>`).join("")}<th>${esc(s.objective)}</th></tr>
      <tr>${Object.values(s.best.params).map(v => `<td class="num">${fmt(v)}</td>`).join("")}<td class="num">${fmt(s.best.metrics[s.objective.split(":")[1]], unitOf(s.objective.split(":")[1]))}</td></tr></table>`;
  }
  if (j.kind === "corners" && s.worst_case) {
    html += `<h3>Worst case over PVT</h3><div class="scroll"><table class="data"><tr><th>metric</th><th>min</th><th>max</th><th>worst</th><th>at</th></tr>` +
      Object.entries(s.worst_case).filter(([, v]) => v.goal).map(([k, v]) => `<tr><td>${esc(k)}</td><td class="num">${fmt(v.min, unitOf(k))}</td>
       <td class="num">${fmt(v.max, unitOf(k))}</td><td class="num">${fmt(v.worst, unitOf(k))}</td><td class="small">${esc(JSON.stringify(v.worst_at))}</td></tr>`).join("") + `</table></div>`;
  }
  if (j.kind === "sweep" || j.kind === "corners" || j.kind === "optimize") {
    html += `<div class="row"><label>Chart metric <select id="jd-metric">${mcols.map(m => `<option>${esc(m)}</option>`).join("")}</select></label>
      ${j.kind === "sweep" ? `<label>x axis <select id="jd-x">${labels.map(l => `<option>${esc(l)}</option>`).join("")}</select></label>` : ""}</div><div id="jd-plot" class="plot"></div>`;
  }
  html += `<h3>Runs</h3><div class="scroll"><table class="data"><tr><th>&#9745;</th><th>#</th>${labels.map(l => `<th>${esc(l)}</th>`).join("")}
     ${mcols.map(m => `<th>${esc(m)}</th>`).join("")}<th>ok</th><th></th></tr>` +
    j.runs.map(r => `<tr><td><input type="checkbox" data-cmp="${r.id}" ${state.compare.has(r.id) ? "checked" : ""}></td><td>${r.idx}</td>
      ${labels.map(l => `<td class="num">${fmt(r.label[l])}</td>`).join("")}
      ${mcols.map(m => `<td class="num">${fmt(r.metrics[m], unitOf(m))}</td>`).join("")}
      <td>${r.ok ? '<span class="ok">✓</span>' : '<span class="bad">✗</span>'}</td><td><button data-run="${r.id}">view</button></td></tr>`).join("") + `</table></div>`;
  el.innerHTML = html;
  $$("input[data-cmp]", el).forEach(cb => cb.onchange = () => { const id = Number(cb.dataset.cmp); cb.checked ? state.compare.add(id) : state.compare.delete(id); saveCompare(); });
  $$("button[data-run]", el).forEach(b => b.onclick = () => openRun(Number(b.dataset.run)));
  const draw = () => drawJobChart(j);
  if ($("#jd-metric")) { $("#jd-metric").onchange = draw; if ($("#jd-x")) $("#jd-x").onchange = draw; draw(); }
  if (j.kind === "simulate" && j.runs.length && !quiet) openRun(j.runs[0].id);
}

function drawJobChart(j) {
  const m = $("#jd-metric").value, el = $("#jd-plot");
  if (j.kind === "sweep") {
    const x = $("#jd-x").value;
    const others = Object.keys(j.runs[0]?.label || {}).filter(k => k !== x);
    const groups = {};
    for (const r of j.runs) { const g = others.map(o => `${o}=${r.label[o]}`).join(", ") || m; (groups[g] ||= []).push(r); }
    const traces = Object.entries(groups).map(([g, rs]) => {
      rs.sort((a, b) => (a.label[x] > b.label[x] ? 1 : -1));
      return { x: rs.map(r => r.label[x]), y: rs.map(r => r.metrics[m]), name: g, mode: "lines+markers" };
    });
    plotCurve(el, { title: `${m} vs ${x}`, xlabel: x, ylabel: `${m} ${unitOf(m)}` }, traces);
  } else if (j.kind === "corners") {
    const lab = j.runs.map(r => `${r.corner}/${(+r.vdd_scale).toFixed(2)}/${r.temperature}°C`);
    plotCurve(el, { title: `${m} over PVT`, ylabel: `${m} ${unitOf(m)}` }, [{ x: lab, y: j.runs.map(r => r.metrics[m]), type: "bar", name: m }]);
  } else {
    let best = null; const goal = (j.summary?.objective || "max:").split(":")[0];
    const ys = j.runs.map(r => r.metrics[m]);
    const bs = ys.map(v => { if (v !== null && (best === null || (goal === "max" ? v > best : v < best))) best = v; return best; });
    plotCurve(el, { title: `${m} per evaluation`, xlabel: "evaluation", ylabel: m }, [
      { x: j.runs.map(r => r.idx), y: ys, mode: "markers", name: m },
      { x: j.runs.map(r => r.idx), y: bs, mode: "lines", name: "best so far (unconstrained)" }]);
  }
}

async function openRun(id) {
  const r = await api(`/api/runs/${id}`);
  const el = $("#run-detail"); el.classList.remove("hidden");
  const ex = r.extra || {};
  const punit = n => ((state.design?.parameters || []).find(p => p.name === n) || {}).unit || "";
  let html = `<h2>Run ${r.idx} <span class="muted small">(id ${r.id})</span> &mdash; ${esc(r.design)} @ ${esc(r.corner)}, ${r.temperature} °C, VDD×${r.vdd_scale}
    <label style="display:inline"><input type="checkbox" id="rd-cmp" ${state.compare.has(r.id) ? "checked" : ""}> compare</label></h2>`;
  if (r.errors?.length) html += `<p class="bad">${r.errors.map(esc).join("<br>")}</p>`;
  if (r.warnings?.length) html += `<p class="warn small">${r.warnings.map(esc).join("<br>")}</p>`;
  html += `<div class="grid2"><div><h3>Metrics</h3><table class="data">${Object.entries(r.metrics).map(([k, v]) => `<tr><td>${esc(k)}</td><td class="num">${fmt(v, unitOf(k))}</td></tr>`).join("")}</table>
    ${ex.phase_noise_method ? `<p class="small muted">phase noise: ${esc(ex.phase_noise_method)}</p>` : ""}</div>
    <div><h3>Parameters</h3><table class="data">${Object.entries(r.params).map(([k, v]) => `<tr><td>${esc(k)}</td><td class="num">${fmt(v, punit(k))}</td></tr>`).join("")}</table></div></div>
    <h3>Curves</h3><div class="plots">${(r.curves || []).map((c, i) => `<div class="plot" id="rc-${i}"></div>`).join("")}</div>
    <details><summary>Netlists (${Object.keys(r.netlists || {}).length})</summary>${Object.entries(r.netlists || {}).map(([k, v]) => `<h3>${esc(k)}</h3><pre class="code">${esc(v)}</pre>`).join("")}</details>
    <details><summary>Simulator runs</summary><pre class="code">${esc(JSON.stringify(r.runs, null, 1))}</pre></details>`;
  el.innerHTML = html;
  (r.curves || []).forEach((c, i) => plotCurve($(`#rc-${i}`), c));
  $("#rd-cmp").onchange = ev => { ev.target.checked ? state.compare.add(r.id) : state.compare.delete(r.id); saveCompare(); };
  el.scrollIntoView({ behavior: "smooth" });
}

/* ---------------------------------------------------------------- compare */
function saveCompare() {
  try { localStorage.setItem("af-compare", JSON.stringify([...state.compare])); } catch { }
  $("#cmp-count").textContent = state.compare.size;
}
async function loadCompare() {
  if (!state.compare.size) { $("#cmp-table").innerHTML = "<tr><td class='muted'>no runs selected</td></tr>"; $("#cmp-plot").innerHTML = ""; $("#cmp-curve").innerHTML = ""; return; }
  const c = await api(`/api/compare?ids=${[...state.compare].join(",")}`);
  const runs = c.runs, first = runs[0];
  const head = `<tr><th></th>${runs.map(r => `<th>#${r.id} ${esc(r.design)}<br><span class="small muted">${esc(r.corner)} ${r.temperature}°C ×${r.vdd_scale}</span></th>`).join("")}</tr>`;
  const mrow = m => `<tr><td>${esc(m)}</td>${runs.map(r => {
    const v = r.metrics[m], v0 = first.metrics[m];
    const d = (r !== first && v !== null && v0) ? ` <span class="small muted">(${((v - v0) / Math.abs(v0) * 100).toFixed(1)}%)</span>` : "";
    return `<td class="num">${fmt(v)}${d}</td>`;
  }).join("")}</tr>`;
  const prow = p => `<tr><td class="muted">${esc(p)}</td>${runs.map(r => `<td class="num muted">${fmt(r.params[p])}</td>`).join("")}</tr>`;
  $("#cmp-table").innerHTML = head + `<tr><th colspan="${runs.length + 1}">metrics</th></tr>` + c.metrics.map(mrow).join("") +
    `<tr><th colspan="${runs.length + 1}">parameters</th></tr>` + c.params.map(prow).join("");
  const names = [...new Set(runs.flatMap(r => (r.curves || []).map(cv => cv.name)))];
  $("#cmp-curve").innerHTML = names.map(n => `<option>${esc(n)}</option>`).join("");
  const draw = () => {
    const name = $("#cmp-curve").value; const traces = []; let ref = null;
    for (const r of runs) { const cv = (r.curves || []).find(x => x.name === name); if (!cv) continue; ref = ref || cv;
      for (const [s, ys] of Object.entries(cv.series)) traces.push({ x: cv.x, y: ys, mode: "lines", name: `#${r.id} ${s}` }); }
    if (ref) plotCurve($("#cmp-plot"), ref, traces);
  };
  $("#cmp-curve").onchange = draw; draw();
}

/* ---------------------------------------------------------------- netlist */
async function genNetlist() {
  const params = {};
  $("#n-params").value.split("\n").map(s => s.trim()).filter(Boolean).forEach(l => { const [k, v] = l.split("="); params[k.trim()] = v.trim(); });
  const out = $("#n-out"); out.innerHTML = "…";
  try {
    const r = await api("/api/netlist", { method: "POST", body: JSON.stringify({ design: $("#n-design").value, backend: $("#n-backend").value,
      pdk: $("#n-pdk").value, corner: $("#n-corner").value || null, params }) });
    out.innerHTML = r.testbenches.map((tb, i) => `<h3>${esc(tb.name)} <span class="muted small">${esc(tb.description)}</span>
      <button class="small-btn" data-dl="${i}">download .${tb.suffix}</button>${tb.ocean ? `<button class="small-btn" data-dlo="${i}">download .ocn</button>` : ""}</h3>
      <pre class="code">${esc(tb.netlist)}</pre>${tb.ocean ? `<details><summary>OCEAN script</summary><pre class="code">${esc(tb.ocean)}</pre></details>` : ""}`).join("");
    const dl = (name, text) => { const a = document.createElement("a"); a.href = URL.createObjectURL(new Blob([text], { type: "text/plain" })); a.download = name; a.click(); };
    $$("button[data-dl]", out).forEach(b => b.onclick = () => { const tb = r.testbenches[b.dataset.dl]; dl(`${tb.name}.${tb.suffix}`, tb.netlist); });
    $$("button[data-dlo]", out).forEach(b => b.onclick = () => { const tb = r.testbenches[b.dataset.dlo]; dl(`${tb.name}.ocn`, tb.ocean); });
  } catch (e) { out.innerHTML = `<p class="bad">${esc(e.message)}</p>`; }
}

/* ---------------------------------------------------------------- system */
async function loadSystem() {
  const i = await api("/api/info");
  $("#sys-out").innerHTML = `<p>version ${esc(i.version)} &middot; data directory <code>${esc(i.data_dir)}</code></p>
    <h3>Backends</h3><table class="data"><tr><th>name</th><th>available</th><th>status</th><th>verification</th></tr>
    ${i.backends.map(b => `<tr><td>${esc(b.name)}</td><td>${b.available ? '<span class="ok">yes</span>' : '<span class="warn">no</span>'}</td>
      <td class="small">${esc(b.status)}</td><td class="small muted" style="white-space:normal">${esc(b.verification)}</td></tr>`).join("")}</table>
    <h3>PDK profiles</h3><table class="data"><tr><th>name</th><th>description</th><th>backend status</th></tr>
    ${i.pdks.map(p => `<tr><td>${esc(p.name)}</td><td class="small">${esc(p.description)}</td><td class="small" style="white-space:normal">${Object.entries(p.backends || {}).map(([b, s]) => `${esc(b)}: ${s.available ? '<span class="ok">ok</span>' : '<span class="warn">' + esc(s.status) + "</span>"}`).join("<br>")}</td></tr>`).join("")}</table>`;
}

/* ---------------------------------------------------------------- init */
async function init() {
  $$("#tabs button").forEach(b => b.onclick = () => showTab(b.dataset.tab));
  try { JSON.parse(localStorage.getItem("af-compare") || "[]").forEach(id => state.compare.add(id)); } catch { }
  saveCompare();
  state.info = await api("/api/info");
  $("#version").textContent = "v" + state.info.version;
  const dopts = state.info.designs.map(d => `<option>${esc(d)}</option>`).join("");
  $("#f-design").innerHTML = dopts; $("#n-design").innerHTML = dopts;
  const bopts = state.info.backends.map(b => `<option ${b.available ? "" : "class='muted'"} value="${b.name}">${b.name}${b.available ? "" : " (netlist only here)"}</option>`).join("");
  $("#f-backend").innerHTML = bopts; $("#n-backend").innerHTML = bopts;
  const firstOk = state.info.backends.find(b => b.available);
  if (firstOk) { $("#f-backend").value = firstOk.name; $("#n-backend").value = firstOk.name; }
  const popts = state.info.pdks.map(p => `<option>${esc(p.name)}</option>`).join("");
  $("#f-pdk").innerHTML = popts; $("#n-pdk").innerHTML = popts;
  $("#f-pdk").value = "sky130"; $("#n-pdk").value = "sky130";
  fillPdk($("#f-pdk"), $("#f-corner"));
  $("#f-pdk").onchange = () => fillPdk($("#f-pdk"), $("#f-corner"));
  $("#f-design").onchange = () => loadDesign($("#f-design").value);
  $("#f-kind").onchange = updateKindPanels; updateKindPanels();
  $("#submit").onclick = submitJob;
  $("#refresh-jobs").onclick = loadJobs;
  $("#cmp-clear").onclick = () => { state.compare.clear(); saveCompare(); loadCompare(); };
  $("#n-go").onclick = genNetlist;
  await loadDesign($("#f-design").value);
}
document.addEventListener("DOMContentLoaded", () => init().catch(e => { document.body.insertAdjacentHTML("afterbegin", `<p class="bad">${esc(e.message)}</p>`); }));
