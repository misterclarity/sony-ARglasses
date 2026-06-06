import type { Component } from "@earendil-works/pi-tui";
import chalk from "chalk";
import type { ProtocolState } from "../state.js";
import { PHASE_NAMES, WIFI_PHASE_NAMES } from "../state.js";

export class HeaderBar implements Component {
  private state: ProtocolState;
  private guardrail: boolean;

  constructor(state: ProtocolState, guardrail: boolean) {
    this.state = state;
    this.guardrail = guardrail;
  }

  update(state: ProtocolState, guardrail: boolean) {
    this.state = state;
    this.guardrail = guardrail;
  }

  invalidate() {}

  render(width: number): string[] {
    const s = this.state;
    const bt = s.btConnected
      ? chalk.green("● connected")
      : chalk.dim("○ off");
    const wifiName = WIFI_PHASE_NAMES[s.wifiPhase] ?? `ph${s.wifiPhase}`;
    const wifi = s.wifiActive
      ? chalk.green(`● ${wifiName}`)
      : s.wifiPhase > 0
        ? chalk.yellow(`◌ ${wifiName}`)
        : chalk.dim("○ off");
    const phaseName = PHASE_NAMES[s.phase] ?? `${s.phase}`;
    const tcp = s.tcpConnected ? chalk.green("✓") : chalk.dim("✗");
    const grLabel = this.guardrail
      ? chalk.bgYellow.black(" ON  ")
      : chalk.dim("[OFF]");

    const parts = [
      `BT: ${bt}`,
      `WiFi: ${wifi}`,
      `Phase: ${chalk.cyan(phaseName)}`,
      `TCP: ${tcp}`,
      `Guardrail: ${grLabel}`,
      chalk.dim("[G]"),
    ];
    const line = " " + parts.join("   ");
    const title = chalk.bold(" GLASSES HARNESS ");
    const bar = `${title}${chalk.dim("─".repeat(Math.max(0, width - title.replace(/\x1b\[[0-9;]*m/g, "").length - 2)))}`;
    return [bar, line];
  }
}
