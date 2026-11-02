const SVG_NS = "http://www.w3.org/2000/svg";

export class MissionAnimator {
  constructor(root = document) {
    this.stage = root.querySelector(".visual-stage");
    this.packetLayer = root.querySelector("#packetLayer");
    this.station = root.querySelector("#stationGroup");
    this.interference = root.querySelector("#interferenceLayer");
    this.spoof = root.querySelector("#spoofIndicator");
    this.motionReduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    this.scenario = null;
  }

  setScenario(scenario) {
    this.scenario = scenario;
    this.stage.classList.remove("fading");
    this.interference.style.opacity = "0";
    this.spoof.style.opacity = "0";
  }

  handle(event) {
    if (event.event_type === "satellite.telemetry_transmitted") {
      this.stage.classList.remove("transmitting");
      void this.stage.offsetWidth;
      this.stage.classList.add("transmitting");
      this.packet("tx", event);
      return;
    }
    if (event.event_type === "channel.rf_observed") {
      this.packet("channel-rx", event);
      // MOCK scenario illustrations begin only after an actual Channel event.
      // They are not detected attacks and never imply a Station outcome.
      if (this.scenario === "jamming") this.interference.style.opacity = "1";
      if (this.scenario === "spoofing") this.spoof.style.opacity = "1";
      if (this.scenario === "replay") this.packet("replay", event);
      if (this.scenario === "malformed") this.packet("tampered", event);
      return;
    }
    if (event.event_type === "channel.action" && event.source_agent_id === "vm3-channel" && event.source_role === "CHANNEL") {
      this.explicitChannelAction(event.disposition, event);
      if (event.disposition === "ATTACK") {
        if (event.attack === "JAMMING" || event.attack === "AWGN") this.interference.style.opacity = "1";
        if (event.attack === "SPOOFING") this.spoof.style.opacity = "1";
        if (event.attack === "REPLAY") this.packet("replay", event);
        if (event.attack === "MALFORMED") this.packet("tampered", event);
      }
      return;
    }
    if (event.event_type === "station.frame_decided" && event.decision_source === "station_receiver") {
      this.station.classList.remove("receiving");
      void this.station.getBBox();
      this.station.classList.add("receiving");
    }
  }

  packet(kind, event) {
    const group = document.createElementNS(SVG_NS, "g");
    group.classList.add("packet", kind);
    group.dataset.eventId = event.event_id;
    const body = document.createElementNS(SVG_NS, "rect");
    body.setAttribute("x", "-22"); body.setAttribute("y", "-10");
    body.setAttribute("width", "44"); body.setAttribute("height", "20"); body.setAttribute("rx", "3");
    body.classList.add("packet-body");
    const label = document.createElementNS(SVG_NS, "text");
    label.setAttribute("text-anchor", "middle"); label.setAttribute("y", "3");
    label.textContent = `#${event.run_sequence ?? event.sequence_number ?? "?"}`;
    group.append(body, label);
    this.packetLayer.append(group);
    const lifespan = this.motionReduced ? 100 : 1900;
    window.setTimeout(() => group.remove(), lifespan);
  }

  explicitChannelAction(action, event) {
    // Reserved for a future schema revision that explicitly authenticates channel actions.
    // Current v1 ChannelEvent has no action field, so the UI never infers these outcomes.
    if (action === "DROP") this.packet("drop", event);
    if (action === "FORWARD") this.packet("forward", event);
    if (action === "ATTACK") this.packet("attack", event);
  }

  reset() {
    this.packetLayer.replaceChildren();
    this.stage.classList.remove("transmitting", "fading");
    this.station.classList.remove("receiving");
    this.interference.style.opacity = "0";
    this.spoof.style.opacity = "0";
  }
}
