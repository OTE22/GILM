"use strict";
const el = id => document.getElementById(id);
async function get(path) {
  const key = el("key").value;
  const response = await fetch(path, {headers: key ? {Authorization: `Bearer ${key}`} : {}});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error?.message || "Request failed");
  return data;
}
function inspect(value) { el("inspection").textContent = JSON.stringify(value, null, 2); }
function row(title, subtitle, onClick) {
  const button = document.createElement("button");
  button.className = "row"; button.type = "button";
  button.append(document.createTextNode(title));
  const small = document.createElement("small"); small.textContent = subtitle; button.append(small);
  button.addEventListener("click", onClick); return button;
}
async function refresh() {
  el("status").textContent = "Loading stored data…";
  try {
    const [health, plans, traces] = await Promise.all([get("/health"), get("/api/plans"), get("/api/traces")]);
    el("mode").textContent = health.default_provider === "mock" ? "MOCK PROVIDER" : health.openrouter_free_only ? "OPENROUTER · FREE MODELS" : "CONFIGURED HTTP PROVIDER";
    el("plans").replaceChildren(); el("traces").replaceChildren();
    for (const plan of plans) el("plans").append(row(`${plan.baseline ? "Baseline" : "Candidate"} · ${plan.active ? "active" : "inactive"}`, `${plan.id.slice(0,14)} · ${plan.description}`, async () => {
      try { inspect({plan, diff: await get(`/api/plans/${plan.id}/diff`), evaluations: await get(`/api/plans/${plan.id}/evaluations`)}); }
      catch (error) { el("status").textContent = error.message; }
    }));
    for (const trace of traces) el("traces").append(row(`${trace.workflow} · ${trace.outcome} · ${trace.cache_status}`, `${trace.mock ? "MOCK" : "HTTP"} · ${trace.latency_ms} ms · ${trace.request_id.slice(0,16)}`, () => inspect(trace)));
    if (!traces.length) el("traces").textContent = "No stored requests yet.";
    el("status").textContent = `${plans.length} plan versions · ${traces.length} recent traces`;
  } catch (error) { el("status").textContent = error.message; }
}
el("refresh").addEventListener("click", refresh);
refresh();
