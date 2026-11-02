import { MissionAnimator } from "/static/animation.js";

const $ = (selector) => document.querySelector(selector);
const animator = new MissionAnimator(document);
const state = { authenticated: false, run: null, socket: null, reconnectTimer: null, reconnects: 0, seen: new Set(), eventCount: 0, supported: new Set() };
const el = {
  connectionChip: $("#connectionChip"), connectionText: $("#connectionText"), utcClock: $("#utcClock"),
  mode: $("#modeValue"), run: $("#runValue"), scenario: $("#scenarioValue"), stream: $("#streamValue"),
  decision: $("#decisionValue"), token: $("#tokenInput"), unlock: $("#unlockButton"), authBlock: $("#authBlock"), authMessage: $("#authMessage"),
  grid: $("#scenarioGrid"), stop: $("#stopButton"), live: $("#liveModeButton"), ledger: $("#eventLedger"),
  eventCount: $("#eventCount"), sequence: $("#sequenceValue"), iq: $("#iqValue"), rf: $("#rfValue"),
  satStatus: $("#satelliteStatus"), satDetail: $("#satelliteDetail"), channelStatus: $("#channelStatus"), channelDetail: $("#channelDetail"),
  stationStatus: $("#stationStatus"), stationDetail: $("#stationDetail"), decisionCard: $("#decisionCard"), decisionCardValue: $("#decisionCardValue"), decisionReason: $("#decisionReason"),
  toast: $("#toastRegion"), watermark: $("#mockWatermark")
};

function setConnection(kind, text) { el.connectionChip.className = `connection-chip ${kind}`; el.connectionText.textContent = text; }
function toast(message, error = false) { const node = document.createElement("div"); node.className = `toast${error ? " error" : ""}`; node.textContent = message; el.toast.append(node); setTimeout(() => node.remove(), 5000); }
function timeTick() { const now = new Date(); el.utcClock.textContent = now.toISOString().slice(11, 19); $("#footerTime").textContent = now.toISOString().replace("T", " // ").slice(0, 22); }
setInterval(timeTick, 1000); timeTick();

async function api(path, options = {}) {
  const response = await fetch(path, { credentials: "same-origin", ...options, headers: { "Content-Type": "application/json", ...(options.headers || {}) } });
  if (!response.ok) { let detail = `${response.status} ${response.statusText}`; try { detail = (await response.json()).detail || detail; } catch {} throw new Error(detail); }
  return response.status === 204 ? null : response.json();
}

async function bootstrap() {
  try {
    const [health, scenarios] = await Promise.all([api("/api/v1/health"), api("/api/v1/scenarios")]);
    state.supported = new Set(scenarios.scenarios);
    setConnection("online", "API ONLINE");
    el.live.disabled = health.live_agents !== "READY";
    validateControls();
  } catch (error) { setConnection("error", "API OFFLINE"); toast(error.message, true); }
}

function validateControls() {
  document.querySelectorAll("[data-scenario]").forEach((button) => {
    if (!state.supported.has(button.dataset.scenario)) { button.disabled = true; button.classList.add("unavailable"); }
  });
}

el.unlock.addEventListener("click", async () => {
  const token = el.token.value;
  if (!token) return toast("Enter the operator token.", true);
  try {
    await api("/api/v1/browser-session", { method: "POST", headers: { Authorization: `Bearer ${token}` }, body: "" });
    el.token.value = ""; state.authenticated = true; el.authBlock.classList.add("authenticated");
    el.authMessage.textContent = "SECURE BROWSER SESSION ACTIVE · TOKEN DISCARDED"; el.unlock.textContent = "ACTIVE"; el.unlock.disabled = true;
    toast("Operator session established.");
  } catch (error) { el.token.value = ""; toast(`Authorization failed: ${error.message}`, true); }
});
el.token.addEventListener("keydown", (event) => { if (event.key === "Enter") el.unlock.click(); });

el.grid.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-scenario]");
  if (!button || button.disabled) return;
  if (!state.authenticated) return toast("Unlock operator controls first.", true);
  if (state.run?.status === "ACTIVE") return toast("Stop the active run before starting another.", true);
  try {
    const run = await api("/api/v1/runs", { method: "POST", body: JSON.stringify({ scenario: button.dataset.scenario, mode: "MOCK" }) });
    activateRun(run, button);
  } catch (error) { toast(error.message, true); }
});

function activateRun(run, button) {
  disconnect(false); state.run = run; state.seen.clear(); state.eventCount = 0; el.ledger.replaceChildren(); animator.reset(); animator.setScenario(run.scenario);
  document.querySelectorAll(".scenario-button").forEach((item) => item.classList.remove("running")); button?.classList.add("running");
  el.mode.textContent = run.mode; el.run.textContent = run.run_id; el.run.title = run.run_id; el.scenario.textContent = run.scenario.toUpperCase();
  el.stop.disabled = run.status !== "ACTIVE"; el.watermark.textContent = run.mode === "MOCK" ? "MOCK TELEMETRY // NO LIVE AGENTS" : "LIVE // NOT READY";
  resetTelemetry(); connectStream();
}

el.stop.addEventListener("click", async () => {
  if (!state.run || !state.authenticated) return;
  try { state.run = await api(`/api/v1/runs/${state.run.run_id}/stop`, { method: "POST", body: "" }); el.stop.disabled = true; disconnect(false); el.stream.textContent = "STOPPED"; document.querySelectorAll(".running").forEach((item) => item.classList.remove("running")); toast("Run stopped safely."); }
  catch (error) { toast(error.message, true); }
});

function connectStream() {
  if (!state.run || state.run.status !== "ACTIVE") return;
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  const socket = new WebSocket(`${protocol}//${location.host}/ws/v1/runs/${state.run.run_id}`);
  state.socket = socket; el.stream.textContent = "CONNECTING";
  socket.addEventListener("open", () => { state.reconnects = 0; el.stream.textContent = "CONNECTED"; setConnection("online", "STREAM ONLINE"); });
  socket.addEventListener("message", ({ data }) => { try { handleEvent(JSON.parse(data)); } catch { toast("Invalid stream payload ignored.", true); } });
  socket.addEventListener("close", () => { if (state.socket !== socket) return; el.stream.textContent = "RECONNECTING"; if (state.run?.status === "ACTIVE") scheduleReconnect(); });
  socket.addEventListener("error", () => socket.close());
}

function scheduleReconnect() { clearTimeout(state.reconnectTimer); const wait = Math.min(1000 * 2 ** state.reconnects++, 15000); state.reconnectTimer = setTimeout(connectStream, wait); }
function disconnect(clearRun = true) { clearTimeout(state.reconnectTimer); if (state.socket) { const old = state.socket; state.socket = null; old.close(); } if (clearRun) state.run = null; }

function handleEvent(event) {
  if (!state.run || event.run_id !== state.run.run_id || state.seen.has(event.event_id)) return;
  state.seen.add(event.event_id); state.eventCount += 1; el.eventCount.textContent = String(state.eventCount).padStart(3, "0"); el.sequence.textContent = event.run_sequence ?? "—";
  animator.handle(event); addLedger(event);
  if (event.event_type === "satellite.telemetry_transmitted") { el.satStatus.textContent = "TX EVENT"; el.satDetail.textContent = `frame ${event.sequence_number ?? "unknown"}`; }
  else if (event.event_type === "channel.rf_observed") { el.channelStatus.textContent = "RX OBSERVED"; el.channelDetail.textContent = event.iq_bytes ? `${event.iq_bytes} IQ bytes` : "MOCK metadata only"; el.iq.textContent = event.iq_bytes ?? "—"; }
  else if (event.event_type === "channel.action" && event.source_agent_id === "vm3-channel" && event.source_role === "CHANNEL") { el.channelStatus.textContent = event.disposition; el.channelDetail.textContent = event.attack || `frame ${event.frame_id}`; }
  else if (event.event_type === "station.frame_decided" && event.decision_source === "station_receiver") { authoritativeDecision(event); }
}

function authoritativeDecision(event) {
  el.stationStatus.textContent = "RX EVENT"; el.stationDetail.textContent = `Station event ${event.station_event_id}`; el.rf.textContent = event.rf_level || "UNKNOWN";
  el.decision.textContent = event.decision; el.decisionCardValue.textContent = event.decision; el.decisionReason.textContent = event.reason || "No reason supplied";
  el.decisionCard.className = `decision-card ${event.decision === "ACCEPT" ? "accept" : "reject"}`;
}

function addLedger(event) {
  const item = document.createElement("li"); const time = document.createElement("time"); const title = document.createElement("strong"); const detail = document.createElement("small");
  time.textContent = `${String(event.run_sequence ?? "—").padStart(3, "0")} · ${new Date(event.event_time).toISOString().slice(11, 19)} UTC`;
  title.textContent = event.event_type.replaceAll(".", " / ").toUpperCase(); detail.textContent = event.event_id;
  item.append(time, title, detail); el.ledger.prepend(item); while (el.ledger.children.length > 50) el.ledger.lastElementChild.remove();
}

function resetTelemetry() {
  el.eventCount.textContent = "000"; el.sequence.textContent = "—"; el.iq.textContent = "—"; el.rf.textContent = "UNKNOWN";
  el.satStatus.textContent = "STANDBY"; el.satDetail.textContent = "Awaiting event"; el.channelStatus.textContent = "PENDING"; el.channelDetail.textContent = "No telemetry";
  el.stationStatus.textContent = "PENDING"; el.stationDetail.textContent = "Authoritative event required"; el.decision.textContent = "PENDING";
  el.decisionCard.className = "decision-card pending"; el.decisionCardValue.textContent = "PENDING"; el.decisionReason.textContent = "No authoritative Station event received";
}

$("#clearLedger").addEventListener("click", () => el.ledger.replaceChildren());
window.addEventListener("beforeunload", () => disconnect(false));
bootstrap();
