"use strict";

// FL Slacker local UI. All data from the companion (including note/track labels and plan
// rationales) is untrusted: it is only ever inserted with textContent, never as HTML.

const $ = (id) => document.getElementById(id);
const SVG = "http://www.w3.org/2000/svg";
const ACTIVE_JOB = new Set(["queued", "awaiting_fl_action", "validating", "applying", "awaiting_verification"]);
const UNRESOLVED = new Set(["outcome_unknown", "partial_apply", "verification_failed"]);
const state = { data: null, detail: null, agentRun: null, loggedIn: false, busy: false };

function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") el.className = value;
    else if (key === "text") el.textContent = value;
    else if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
    else el.setAttribute(key, value === true ? "" : String(value));
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

function s(tag, attrs = {}) {
  const el = document.createElementNS(SVG, tag);
  for (const [key, value] of Object.entries(attrs)) el.setAttribute(key, String(value));
  return el;
}

function toast(message, isError = false) {
  const el = $("toast");
  el.textContent = message;
  el.className = "toast show" + (isError ? " error" : "");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { el.className = "toast"; }, isError ? 7000 : 3500);
}

function short(id) { return id ? String(id).slice(0, 8) : "-"; }

function age(seconds) {
  if (seconds === null || seconds === undefined) return "unknown age";
  if (seconds < 60) return `${seconds}s old`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min old`;
  return `${Math.floor(seconds / 3600)} h old`;
}

function since(iso, nowIso) {
  if (!iso) return null;
  return Math.max(0, Math.round((Date.parse(nowIso) - Date.parse(iso)) / 1000));
}

function fill(el, ...children) {
  el.replaceChildren(...children.flat().filter((c) => c !== null && c !== undefined && c !== false));
}

function pill(text, kind = "") { return h("span", { class: `pill ${kind}`, text }); }

function stateKind(value) {
  if (value === "applied" || value === "completed") return "ok";
  if (ACTIVE_JOB.has(value)) return "warn";
  if (UNRESOLVED.has(value) || value === "failed") return "bad";
  return "";
}

class ApiError extends Error {
  constructor(message, code, status) { super(message); this.code = code; this.status = status; }
}

async function api(method, path, body) {
  const options = { method, credentials: "same-origin", headers: { "X-FLS-CSRF": "1" } };
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  let data = null;
  try { data = await response.json(); } catch (_) { data = null; }
  if (response.status === 401) {
    showLogin();
    throw new ApiError("Please sign in.", "PERMISSION_DENIED", 401);
  }
  if (!response.ok && !(data && "ok" in data && data.ok === false && data.error && path.startsWith("/v1/tools/"))) {
    const error = (data && data.error) || { code: "ERROR", message: `HTTP ${response.status}` };
    throw new ApiError(`${error.code}: ${error.message}`, error.code, response.status);
  }
  return data;
}

async function tool(name, args) {
  const result = await api("POST", `/v1/tools/${name}`, args);
  if (!result.ok) throw new ApiError(`${result.error.code}: ${result.error.message}`, result.error.code);
  return result.data;
}

async function guarded(action, success) {
  if (state.busy) return;
  state.busy = true;
  try {
    const value = await action();
    if (success) toast(typeof success === "function" ? success(value) : success);
    await refresh();
    return value;
  } catch (error) {
    if (error.status !== 401) toast(error.message, true);
  } finally {
    state.busy = false;
  }
}

// ------------------------------------------------------------------ login

function showLogin() {
  state.loggedIn = false;
  $("app").hidden = true;
  $("login").hidden = false;
  $("logout").hidden = true;
  $("new-session").hidden = true;
  $("connection").replaceChildren();
}

async function login(event) {
  event.preventDefault();
  $("login-error").textContent = "";
  const response = await fetch("/v1/ui/login", {
    method: "POST", credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-FLS-CSRF": "1" },
    body: JSON.stringify({ code: $("login-code").value }),
  });
  if (!response.ok) {
    $("login-error").textContent = "That code is invalid or expired. Run `flslacker ui` again.";
    return;
  }
  $("login-code").value = "";
  await refresh();
}

// ------------------------------------------------------------------ rendering

function renderConnection(data) {
  const caps = data.capabilities;
  const session = caps.active_session;
  const bridge = caps.bridge;
  const contact = since(bridge.last_fl_contact_at, data.now);
  const parts = [
    session ? pill(`session ${short(session.session_id)}`) : pill("no session", "bad"),
    pill(`FL ${caps.fl_build || "unknown"}`),
    bridge.connected ? pill(`last FL contact ${age(contact)}`, "ok")
      : pill("FL has not reported in this session", "warn"),
  ];
  const patch = caps.capabilities.find((c) => c.name === "notes.patch.update");
  if (patch && !patch.supported) parts.push(pill("writes unavailable (unverified build)", "bad"));
  else if (patch && !patch.verified_build) parts.push(pill("EXPERIMENTAL: unverified host", "warn"));
  else if (patch) parts.push(pill(`verified on ${patch.verified_build}`, "ok"));
  if (data.host_status) parts.push(pill("MIDI metadata connected", "ok"));
  $("connection").replaceChildren(...parts);
}

function renderFlActions(data) {
  const waiting = data.jobs.filter((j) => j.state === "awaiting_fl_action" && j.next_action);
  const verifying = data.jobs.filter((j) => j.state === "awaiting_verification" && j.next_action);
  const items = [...waiting, ...verifying].map((job) => h("div", { class: "callout" },
    h("div", { class: "row" },
      h("span", { class: "script", text: job.next_action.script || "Action needed" }),
      job.next_action.request_short_id ? h("span", { class: "code", text: job.next_action.request_short_id }) : null,
      h("span", { class: "spacer" }),
      pill(job.purpose)),
    h("p", { text: job.next_action.instruction })));
  $("fl-actions").replaceChildren(...items);
}

function renderRecipes(data) {
  // Cards are built once (so typing is not interrupted); only their status lines refresh.
  const container = $("recipes");
  const ids = data.recipes.map((r) => r.recipe_id).join(",");
  if (container.dataset.ids !== ids) {
    container.dataset.ids = ids;
    const nodes = data.recipes.map((recipe) => {
      const input = h("input", { maxlength: 120, placeholder: "Target label (optional)",
        "aria-label": `Target label for ${recipe.name}` });
      return h("div", { class: "item" },
        h("div", { class: "title", text: recipe.name }),
        h("p", { class: "meta", text: recipe.description }),
        h("div", { class: "row" }, input, h("button", {
          type: "button",
          onclick: () => {
            const session = state.data && state.data.capabilities.active_session;
            if (!session) { toast("No active session.", true); return; }
            guarded(() => api("POST", `/v1/ui/recipes/${recipe.recipe_id}/run`, {
              session_id: session.session_id, target_label: input.value.trim() || null,
            }), "Recipe started: follow the FL action above.");
          },
        }, "Run")),
        h("p", { class: "meta", "data-run-status": recipe.recipe_id }));
    });
    fill(container, nodes.length ? nodes : [h("p", { class: "empty", text: "No recipes." })]);
  }
  for (const line of container.querySelectorAll("[data-run-status]")) {
    const run = (data.recipe_runs || []).find((r) => r.recipe_id === line.dataset.runStatus);
    line.textContent = run
      ? `Last run: ${run.apply_state ? `apply ${run.apply_state}` : run.state}`
        + (run.messages.length ? ` · ${run.messages.slice(-2).join("; ")}` : "")
      : "";
  }
}

function renderSnapshots(data) {
  const nodes = data.snapshots.map((snap) => h("button", {
    class: "item", type: "button", onclick: () => showSnapshot(snap.snapshot_id),
  },
  h("div", { class: "row" },
    h("span", { class: "title", text: snap.target }),
    h("span", { class: "spacer" }),
    snap.is_latest ? pill("latest", "ok") : pill("superseded", "warn")),
  h("div", { class: "meta", text: `${snap.in_scope}/${snap.notes} notes in scope '${snap.scope}' · ${snap.purpose} · cached, ${age(snap.age_seconds)}` })));
  $("snapshots").replaceChildren(...(nodes.length ? nodes : [h("p", { class: "empty", text: "No captures in this session yet." })]));
}

function renderJobs(data) {
  const nodes = data.jobs.slice(0, 20).map((job) => {
    const buttons = [];
    if (job.state === "queued" || job.state === "awaiting_fl_action") {
      buttons.push(h("button", { class: "ghost", type: "button",
        onclick: () => guarded(() => tool("fls_cancel_job", { job_id: job.job_id }), "Cancel requested.") }, "Cancel"));
    }
    if (UNRESOLVED.has(job.state) || job.state === "awaiting_verification") {
      buttons.push(h("button", { class: "danger", type: "button", onclick: () => {
        const note = prompt("Describe what you checked in FL. This releases the writer lock without verification.");
        if (note) guarded(() => api("POST", `/v1/ui/jobs/${job.job_id}/acknowledge`, { note }), "Job acknowledged.");
      } }, "Acknowledge"));
    }
    if (job.plan_id) {
      buttons.push(h("button", { class: "ghost", type: "button", onclick: () => showPlan(job.plan_id) }, "Plan"));
    }
    return h("div", { class: "item" },
      h("div", { class: "row" },
        h("span", { class: "title", text: `${job.kind} ${short(job.job_id)}` }),
        pill(job.state, stateKind(job.state)),
        h("span", { class: "spacer" }),
        h("span", { class: "meta", text: job.purpose })),
      job.target_label ? h("div", { class: "meta", text: `target '${job.target_label}' · by ${job.created_by}` }) : null,
      job.error ? h("p", { class: "error", text: `${job.error.code}: ${job.error.message}` }) : null,
      buttons.length ? h("div", { class: "row end" }, buttons) : null);
  });
  $("jobs").replaceChildren(...(nodes.length ? nodes : [h("p", { class: "empty", text: "No jobs." })]));
}

function renderPlans(data) {
  const nodes = data.plans.map((plan) => h("button", {
    class: "item", type: "button", onclick: () => showPlan(plan.plan_id),
  },
  h("div", { class: "row" },
    h("span", { class: "title", text: `${plan.kind} ${short(plan.plan_id)}: ${plan.summary}` }),
    h("span", { class: "spacer" }),
    plan.expired ? pill("expired") : null,
    plan.approval && plan.approval.status === "pending" ? pill(`${plan.approval.requested_by} asks to apply`, "warn") : null),
  h("div", { class: "meta", text: `'${plan.target}' · by ${plan.created_by}` })));
  $("plans").replaceChildren(...(nodes.length ? nodes : [h("p", { class: "empty", text: "No plans yet." })]));
}

function renderReceipts(data) {
  const nodes = data.receipts.map((receipt) => h("div", { class: "item" },
    h("div", { class: "row" },
      h("span", { class: "title", text: `Receipt ${short(receipt.receipt_id)}: ${receipt.applied_operation_count} operations on '${receipt.target_label}'` }),
      pill("verified", "ok")),
    h("ul", {}, receipt.changes.slice(0, 6).map((c) => h("li", { class: "meta", text: c.summary }))),
    h("p", { class: "meta", text: receipt.recovery_note }),
    h("div", { class: "row end" }, h("button", {
      class: "ghost", type: "button", disabled: !receipt.recovery_available,
      onclick: () => guarded(async () => {
        const plan = await api("POST", `/v1/ui/receipts/${receipt.receipt_id}/restore-plan`);
        await showPlan(plan.plan_id);
      }, "Restore plan ready: review it, then approve."),
    }, "Prepare restore"))));
  $("receipts").replaceChildren(...(nodes.length ? nodes : [h("p", { class: "empty", text: "No verified edits yet." })]));
}

function renderGrants(data) {
  const now = Date.parse(data.now);
  const nodes = data.grants.slice().reverse().map((grant) => {
    const active = !grant.revoked && Date.parse(grant.expires_at) > now;
    const c = grant.constraints;
    return h("div", { class: "item" },
      h("div", { class: "row" },
        h("span", { class: "title", text: `${grant.kind} grant ${short(grant.grant_id)}` }),
        active ? pill("active", "ok") : pill(grant.revoked ? "revoked" : "expired"),
        h("span", { class: "spacer" }),
        active ? h("button", { class: "ghost", type: "button",
          onclick: () => guarded(() => api("POST", `/v1/ui/grants/${grant.grant_id}/revoke`), "Grant revoked.") }, "Revoke") : null),
      h("div", { class: "meta", text:
        `${c.operation_kinds.join(", ")} · fields ${c.fields.join(", ") || "-"} · ${grant.budget_used}/${c.max_notes} notes used`
        + (c.max_pitch_delta !== null ? ` · ≤${c.max_pitch_delta} semitones` : "")
        + ` · scope ${c.scope}` + (grant.target_label ? ` · '${grant.target_label}'` : " · any target")
        + ` · until ${new Date(grant.expires_at).toLocaleTimeString()}` }));
  });
  $("grants").replaceChildren(...(nodes.length ? nodes : [h("p", { class: "empty", text: "No grants. Agents cannot write." })]));
}

function renderCapabilities(data) {
  const rows = data.capabilities.capabilities.map((c) => h("tr", {},
    h("td", { class: "mono", text: c.name }),
    h("td", {}, pill(c.status, c.supported ? (c.status === "supported" ? "ok" : "warn") : "bad")),
    h("td", { text: c.verified_build || "-" }),
    h("td", { class: "meta", text: [c.reason, ...(c.limitations || [])].filter(Boolean).join(" · ") })));
  $("capabilities").replaceChildren(h("div", { class: "table-wrap" }, h("table", {},
    h("thead", {}, h("tr", {}, h("th", { text: "Capability" }), h("th", { text: "Status" }),
      h("th", { text: "Verified build" }), h("th", { text: "Notes" }))),
    h("tbody", {}, rows))));
}

function render() {
  const data = state.data;
  renderConnection(data);
  renderFlActions(data);
  renderRecipes(data);
  renderSnapshots(data);
  renderJobs(data);
  renderPlans(data);
  renderReceipts(data);
  renderGrants(data);
  renderCapabilities(data);
  $("agent-card").hidden = !data.settings.ollama_model;
}

async function refresh() {
  try {
    const data = await api("GET", "/v1/ui/state");
    const first = !state.loggedIn;
    state.data = data;
    state.loggedIn = true;
    $("login").hidden = true;
    $("app").hidden = false;
    $("logout").hidden = false;
    $("new-session").hidden = false;
    render();
    if (first && data.snapshots.length) showSnapshot(data.snapshots[0].snapshot_id);
  } catch (error) {
    if (error.status !== 401) toast(error.message, true);
  }
}

// ------------------------------------------------------------------ piano roll

function pianoRoll(notes) {
  const svg = s("svg", { class: "roll", viewBox: "0 0 1000 220", preserveAspectRatio: "none", role: "img" });
  if (!notes.length) return svg;
  const pitches = notes.map((n) => n.pitch);
  const low = Math.min(...pitches) - 2;
  const high = Math.max(...pitches) + 2;
  const end = Math.max(...notes.map((n) => n.start + Math.max(n.duration, 1)), 1);
  const rowH = 220 / (high - low + 1);
  svg.setAttribute("aria-label", `Piano roll of ${notes.length} notes, pitches ${low + 2} to ${high - 2}`);
  for (const n of notes) {
    const rect = s("rect", {
      x: (n.start / end) * 1000,
      y: (high - n.pitch) * rowH + 1,
      width: Math.max(3, (Math.max(n.duration, 1) / end) * 1000 - 1),
      height: Math.max(2, rowH - 2),
      rx: 1.5,
      class: n.cls,
    });
    const title = s("title");
    title.textContent = n.label;
    rect.append(title);
    svg.append(rect);
  }
  return svg;
}

function legend(items) {
  return h("div", { class: "legend" }, items.map(([cls, text]) => h("span", {}, h("span", { class: `swatch ${cls}` }), text)));
}

// ------------------------------------------------------------------ snapshot detail

async function showSnapshot(snapshotId) {
  let snap;
  try { snap = await api("GET", `/v1/ui/snapshots/${snapshotId}`); } catch (error) { toast(error.message, true); return; }
  state.detail = { type: "snapshot", id: snapshotId };
  const notes = snap.notes.map((n) => ({
    pitch: n.pitch, start: n.start_tick, duration: n.duration_tick,
    cls: n.in_scope ? "n-after" : "n-out",
    label: `${n.note_id}: pitch ${n.pitch} @ ${n.start_tick} (${n.duration_tick} ticks)`,
  }));
  const fresh = snap.freshness;
  const findings = h("div", {});
  const keyHint = h("input", { maxlength: 20, placeholder: "e.g. C major (optional constraint)" });
  const kinds = ["motifs", "key", "chords", "pitch_outliers"].map((kind) => h("label", {},
    h("input", { type: "checkbox", value: kind, checked: kind !== "chords" }), ` ${kind}`));

  const analyze = async () => {
    const analyses = kinds.map((l) => l.querySelector("input")).filter((i) => i.checked).map((i) => i.value);
    if (!analyses.length) { toast("Choose at least one analysis.", true); return; }
    const parameters = keyHint.value.trim() ? { key_hint: keyHint.value.trim() } : {};
    try {
      const analysis = await tool("fls_analyze_score", { snapshot_id: snapshotId, analyses, parameters });
      renderFindings(findings, analysis, snap);
    } catch (error) { toast(error.message, true); }
  };

  const noteRows = snap.notes.slice(0, 400).map((n) => h("tr", {},
    h("td", { class: "mono", text: n.note_id }), h("td", { class: "num", text: n.pitch }),
    h("td", { class: "num", text: n.start_tick }), h("td", { class: "num", text: n.duration_tick }),
    h("td", { class: "num", text: typeof n.velocity === "number" ? n.velocity.toFixed(3) : n.velocity }),
    h("td", { text: n.in_scope ? "yes" : "no" }),
    h("td", { class: "meta", text: Object.entries(n.fl).map(([k, v]) => `${k}=${v}`).join(" ") })));

  fill($("detail"),
    h("div", { class: "row" },
      h("h2", { text: `Snapshot of '${snap.target.label}'` }),
      h("span", { class: "spacer" }),
      pill(`cached · ${age(fresh.age_seconds)}`, "warn"),
      fresh.is_latest_for_target ? pill("latest capture", "ok") : pill("superseded", "bad")),
    h("p", { class: "meta", text: `${snap.in_scope_count} of ${snap.selection.exposed_count} exposed notes in scope '${snap.scope}' · PPQ ${snap.ppq} · target binding ${snap.target.binding} · state ${snap.state_hash.slice(0, 12)}` }),
    Object.keys(snap.unsupported_fields).length
      ? h("p", { class: "error", text: `Unreadable fields: ${Object.keys(snap.unsupported_fields).join(", ")}` }) : null,
    pianoRoll(notes),
    legend([["after", "in scope"], ["before", "outside scope"]]),
    h("div", { class: "row" }, ...kinds),
    h("div", { class: "row" }, keyHint, h("button", { type: "button", onclick: analyze }, "Analyze")),
    findings,
    h("details", {}, h("summary", { text: `Notes (${snap.notes.length})` }),
      h("div", { class: "table-wrap" }, h("table", {},
        h("thead", {}, h("tr", {}, ...["id", "pitch", "start", "length", "velocity", "in scope", "other FL properties"].map((t) => h("th", { text: t })))),
        h("tbody", {}, noteRows)))));
}

function renderFindings(container, analysis, snap) {
  const checks = [];
  const rows = analysis.findings.map((finding) => {
    let box = null;
    if (finding.suggestion) {
      box = h("input", { type: "checkbox", checked: finding.confidence >= 0.6, "aria-label": `use suggestion ${finding.finding_id}` });
      checks.push([box, finding]);
    }
    const fill = h("span", {});
    fill.style.width = `${Math.round(finding.confidence * 100)}%`;  // CSSOM is allowed by the CSP
    return h("div", { class: "finding" },
      box || h("span", {}),
      h("div", {},
        h("div", {}, pill(finding.kind), " ", finding.message),
        finding.alternatives && finding.alternatives.length && finding.kind === "key_hypotheses"
          ? h("div", { class: "meta", text: finding.alternatives.map((a) => `${a.key} (${a.score})`).join(" · ") }) : null),
      h("div", { title: `confidence ${finding.confidence}` },
        h("div", { class: "bar" }, fill)));
  });
  const summary = analysis.summary;
  const propose = h("button", { type: "button", disabled: !checks.length, onclick: async () => {
    const chosen = checks.filter(([box]) => box.checked).map(([, f]) => f.suggestion);
    if (!chosen.length) { toast("Tick at least one suggestion.", true); return; }
    try {
      const plan = await tool("fls_propose_patch", {
        snapshot_id: snap.snapshot_id,
        operations: chosen,
        rationale: `Selected ${chosen.length} analysis suggestion(s) in the UI.`,
      });
      await refresh();
      await showPlan(plan.plan_id);
    } catch (error) { toast(error.message, true); }
  } }, "Propose selected suggestions");
  fill(container,
    h("h3", { text: "Findings" }),
    h("p", { class: "meta", text: `${summary.analyzed_notes} notes analysed · key ${summary.key.key || "unknown"} (${summary.key.source}) · ${summary.motif_count ?? 0} motifs · suggestions are not certainty` }),
    rows.length ? h("div", {}, rows) : h("p", { class: "empty", text: "No findings." }),
    h("div", { class: "row end" }, propose));
}

// ------------------------------------------------------------------ plan detail

async function showPlan(planId) {
  let preview;
  let snap;
  try {
    preview = await api("GET", `/v1/ui/plans/${planId}`);
    snap = await api("GET", `/v1/ui/snapshots/${preview.plan.snapshot_id}`);
  } catch (error) { toast(error.message, true); return; }
  state.detail = { type: "plan", id: planId };
  const plan = preview.plan;
  const limits = plan.limits;

  const byId = new Map(snap.notes.map((n) => [n.note_id, n]));
  const updated = new Map();
  const deleted = new Set();
  const notes = [];
  for (const row of plan.diff) {
    if (row.kind === "update") updated.set(row.note_id, row.after);
    if (row.kind === "delete") deleted.add(row.note_id);
  }
  for (const n of snap.notes) {
    const label = `${n.note_id}: pitch ${n.pitch} @ ${n.start_tick}`;
    if (deleted.has(n.note_id)) {
      notes.push({ pitch: n.pitch, start: n.start_tick, duration: n.duration_tick, cls: "n-deleted", label: `${label} (deleted)` });
    } else if (updated.has(n.note_id)) {
      const after = updated.get(n.note_id);
      notes.push({ pitch: n.pitch, start: n.start_tick, duration: n.duration_tick, cls: "n-before", label: `${label} (before)` });
      notes.push({ pitch: after.pitch ?? n.pitch, start: after.start_tick ?? n.start_tick,
        duration: after.duration_tick ?? n.duration_tick, cls: "n-changed", label: `${n.note_id} after` });
    } else {
      notes.push({ pitch: n.pitch, start: n.start_tick, duration: n.duration_tick, cls: n.in_scope ? "n-after" : "n-out", label });
    }
  }
  for (const row of plan.diff.filter((r) => r.kind === "insert")) {
    notes.push({ pitch: row.after.pitch, start: row.after.start_tick, duration: row.after.duration_tick, cls: "n-changed", label: `${row.client_id} (new)` });
  }

  const diffRows = [];
  for (const row of plan.diff) {
    const fields = row.kind === "update" ? row.changed_fields : ["pitch", "start_tick", "duration_tick", "velocity"];
    for (const field of fields) {
      diffRows.push(h("tr", {},
        h("td", { text: row.kind }),
        h("td", { class: "mono", text: row.note_id || row.client_id }),
        h("td", { text: field }),
        h("td", { class: "num", text: row.before ? String(row.before[field]) : "—" }),
        h("td", { class: "num changed", text: row.after ? String(row.after[field]) : "—" })));
    }
  }
  const coverage = preview.grant_coverage;
  const blocked = preview.expired || !preview.snapshot_is_latest;
  const apply = h("button", { type: "button", disabled: blocked,
    onclick: () => guarded(() => api("POST", `/v1/ui/plans/${planId}/apply`), "Apply requested: follow the FL action.") },
  coverage.covered ? "Apply" : "Approve & apply");
  const approve = h("button", { class: "ghost", type: "button", disabled: blocked || coverage.covered,
    onclick: () => guarded(() => api("POST", `/v1/ui/plans/${planId}/approve`, { lifetime_minutes: 10 }),
      "Approved for 10 minutes; the agent may now apply this exact plan.") }, "Approve only");

  const planJobs = state.data ? state.data.jobs.filter((j) => j.plan_id === planId) : [];
  const planJob = planJobs.length ? planJobs[0] : null;
  fill($("detail"),
    h("div", { class: "row" },
      h("h2", { text: `${plan.kind === "restore" ? "Restore plan" : "Plan"} ${short(plan.plan_id)} on '${plan.target_label}'` }),
      h("span", { class: "spacer" }),
      preview.expired ? pill("expired", "bad") : pill(`expires ${new Date(plan.expires_at).toLocaleTimeString()}`),
      planJob ? pill(`job ${short(planJob.job_id)}: ${planJob.state}`, stateKind(planJob.state)) : null,
      preview.snapshot_is_latest ? pill("base is latest capture", "ok")
        : pill(planJob && planJob.state === "applied" ? "base superseded by the verified edit" : "stale base",
          planJob && planJob.state === "applied" ? "" : "bad")),
    h("p", { class: "meta", text: `${limits.update_count} updates · ${limits.insert_count} inserts · ${limits.delete_count} deletes · fields ${limits.fields.join(", ") || "-"} · max pitch change ${limits.max_pitch_delta} · by ${plan.created_by} · hash ${plan.plan_hash.slice(0, 12)}` }),
    h("p", {}, h("strong", { text: "Rationale (from the proposer): " }), plan.rationale),
    preview.warnings.length ? h("ul", { class: "warnings" }, preview.warnings.map((w) => h("li", { text: w }))) : null,
    pianoRoll(notes),
    legend([["after", "unchanged"], ["before", "before"], ["changed", "after / new"], ["deleted", "deleted"]]),
    h("div", { class: "table-wrap" }, h("table", {},
      h("thead", {}, h("tr", {}, ...["change", "note", "field", "before", "after"].map((t) => h("th", { text: t })))),
      h("tbody", {}, diffRows))),
    h("p", { class: coverage.covered ? "meta" : "error", text: coverage.reason }),
    h("div", { class: "row end" }, approve, apply));
}

// ------------------------------------------------------------------ forms

async function captureSubmit(event) {
  event.preventDefault();
  const scope = new FormData(event.target).get("scope");
  const session = state.data && state.data.capabilities.active_session;
  if (!scope || !session) { toast("Choose a scope first.", true); return; }
  const label = $("capture-label").value.trim();
  await guarded(() => tool("fls_capture_score", {
    session_id: session.session_id, scope, target_label: label || null,
  }), "Capture requested: run Slacker Capture in FL.");
}

async function grantSubmit(event) {
  event.preventDefault();
  const form = new FormData(event.target);
  const session = state.data && state.data.capabilities.active_session;
  if (!session) return;
  const body = {
    session_id: session.session_id,
    target_label: form.get("target_label").trim() || null,
    lifetime_minutes: Number(form.get("lifetime_minutes")),
    constraints: {
      scope: form.get("scope"),
      operation_kinds: form.getAll("op"),
      fields: form.getAll("field"),
      max_notes: Number(form.get("max_notes")),
      max_pitch_delta: Number(form.get("max_pitch_delta")),
    },
  };
  await guarded(() => api("POST", "/v1/ui/grants", body), "Grant issued.");
}

async function agentSubmit(event) {
  event.preventDefault();
  const prompt = $("agent-prompt").value.trim();
  if (!prompt) return;
  try {
    const run = await api("POST", "/v1/ui/agent", { prompt });
    state.agentRun = run.run_id;
    pollAgent();
  } catch (error) { toast(error.message, true); }
}

async function pollAgent() {
  if (!state.agentRun) return;
  try {
    const run = await api("GET", `/v1/ui/agent/${state.agentRun}`);
    const lines = run.tool_calls.map((c) => `• ${c.tool}: ${c.ok ? "ok" : c.code}`);
    if (run.final) lines.push("", run.final);
    if (run.error) lines.push("", `Stopped: ${run.error.code}: ${run.error.message}`);
    if (run.state !== "running" && !run.final && !run.error) lines.push("", `Stopped: ${run.stop_reason}`);
    $("agent-output").textContent = lines.join("\n") || "Thinking…";
    if (run.state === "running") setTimeout(pollAgent, 1500);
    else refresh();
  } catch (error) { toast(error.message, true); }
}

document.addEventListener("DOMContentLoaded", () => {
  $("login-form").addEventListener("submit", login);
  $("capture-form").addEventListener("submit", captureSubmit);
  $("grant-form").addEventListener("submit", grantSubmit);
  $("agent-form").addEventListener("submit", agentSubmit);
  $("logout").addEventListener("click", async () => {
    await fetch("/v1/ui/logout", { method: "POST", credentials: "same-origin", headers: { "X-FLS-CSRF": "1" } });
    showLogin();
  });
  $("new-session").addEventListener("click", () => {
    if (confirm("Start a new session? Pending FL requests will expire and plans from this session become unusable.")) {
      guarded(() => api("POST", "/v1/ui/session/new"), "New session started.");
    }
  });
  refresh();
  setInterval(() => { if (state.loggedIn && !document.hidden && !state.busy) refresh(); }, 2000);
});
