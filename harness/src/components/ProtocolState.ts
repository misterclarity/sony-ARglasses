import type { Component } from "@earendil-works/pi-tui";
import chalk from "chalk";
import type { ProtocolState } from "../state.js";
import { PHASE_NAMES, WIFI_PHASE_NAMES } from "../state.js";

export class ProtocolStatePanel implements Component {
  private state: ProtocolState;

  constructor(state: ProtocolState) {
    this.state = state;
  }

  update(state: ProtocolState) {
    this.state = state;
  }

  invalidate() {}

  render(width: number): string[] {
    const s = this.state;
    const phaseName = `${s.phase} (${PHASE_NAMES[s.phase] ?? "?"})`;
    const wifiPhaseName = `${s.wifiPhase} (${WIFI_PHASE_NAMES[s.wifiPhase] ?? "?"})`;
    const btStatus = s.btConnected ? chalk.green("connected ch4") : chalk.dim("disconnected");
    const tcpStatus = s.tcpConnected ? chalk.green("connected") : chalk.dim("not connected");
    const wifiStatus = s.wifiActive ? chalk.green("ACTIVE") : chalk.dim("inactive");
    const avgRatio = s.avgCompressionRatio > 0
      ? `${(s.avgCompressionRatio * 100).toFixed(1)}% avg`
      : "n/a";

    const rows: Array<[string, string]> = [
      ["Phase:", phaseName],
      ["WiFi Phase:", wifiPhaseName],
      ["BT:", btStatus],
      ["TCP:", tcpStatus],
      ["WiFi:", wifiStatus],
      ["Frames sent:", String(s.framesSent)],
      ["Compression:", avgRatio],
    ];

    const lines: string[] = [chalk.bold(" PROTOCOL STATE")];
    for (const [label, value] of rows) {
      const lbl = chalk.dim(label.padEnd(14));
      lines.push(` ${lbl} ${value}`);
    }

    if (s.lastEvent) {
      lines.push("");
      lines.push(chalk.dim(` Last: ${s.lastEvent.type} ${
        "cmd" in s.lastEvent ? s.lastEvent.cmd : ""
      }`));
    }
    return lines;
  }
}
