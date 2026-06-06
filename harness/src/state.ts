import type { GlassesEvent, StateEvent, WifiEvent } from "./events.js";

export interface ProtocolState {
  phase: number;
  wifiPhase: number;
  wifiActive: boolean;
  tcpConnected: boolean;
  btConnected: boolean;
  framesSent: number;
  compressSamples: number[];
  avgCompressionRatio: number;
  lastEvent?: GlassesEvent;
}

export const PHASE_NAMES: Record<number, string> = {
  0: "init",
  1: "proto_ver",
  2: "settings",
  3: "version",
  4: "new_host_app",
  5: "display_ready",
};

export const WIFI_PHASE_NAMES: Record<number, string> = {
  0: "off",
  10: "turning_on",
  11: "wifi_enabled",
  12: "tcp_connecting",
  13: "wifi_active",
};

export function makeInitialState(): ProtocolState {
  return {
    phase: 0,
    wifiPhase: 0,
    wifiActive: false,
    tcpConnected: false,
    btConnected: false,
    framesSent: 0,
    compressSamples: [],
    avgCompressionRatio: 0,
  };
}

export function applyEvent(state: ProtocolState, event: GlassesEvent): ProtocolState {
  const next = { ...state, lastEvent: event };

  if (event.type === "STATE") {
    const s = event as StateEvent;
    next.phase = s.phase;
    next.wifiPhase = s.wifi_phase;
    next.wifiActive = s.wifi_active;
    next.tcpConnected = s.tcp_connected;
    next.btConnected = s.bt_connected;
  } else if (event.type === "TX") {
    if (event.cmd === "0xe7") {
      next.framesSent = state.framesSent + 1;
    }
    if (!state.btConnected && event.phase >= 0) {
      next.btConnected = true;
    }
  } else if (event.type === "WIFI") {
    const w = event as WifiEvent;
    switch (w.event) {
      case "ENABLED":   next.wifiPhase = 11; break;
      case "CONNECTED": next.wifiPhase = 12; break;
      case "SWITCHED":  next.wifiPhase = 13; next.wifiActive = true; break;
      case "DROPPED":   next.wifiPhase = 0;  next.wifiActive = false; next.tcpConnected = false; break;
    }
  } else if (event.type === "COMPRESS") {
    const samples = [...state.compressSamples, event.ratio].slice(-50);
    next.compressSamples = samples;
    next.avgCompressionRatio = samples.reduce((a, b) => a + b, 0) / samples.length;
  }

  return next;
}
