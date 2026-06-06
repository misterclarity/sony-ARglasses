import type { Component } from "@earendil-works/pi-tui";
import chalk from "chalk";
import type { GlassesEvent } from "../events.js";

const MAX_ENTRIES = 500;

export class EventLog implements Component {
  private entries: string[] = [];
  private scrollOffset = 0; // lines from bottom (0 = bottom)

  invalidate() {}

  addEvent(e: GlassesEvent) {
    const t = new Date(e.ts);
    const mm = String(t.getMinutes()).padStart(2, "0");
    const ss = String(t.getSeconds()).padStart(2, "0");
    const ms = String(t.getMilliseconds()).padStart(3, "0");
    const ts = `${mm}:${ss}.${ms}`;

    let line: string;
    switch (e.type) {
      case "TX": {
        const kb = (e.bytes / 1024).toFixed(1);
        const ok = e.ok ? chalk.green("✓") : chalk.red("✗");
        const name = e.name.length > 22 ? e.name.slice(0, 22) : e.name;
        line = `${chalk.dim(ts)} ${chalk.blue("TX")} ${chalk.cyan(e.cmd)} ${chalk.white(name)} ${chalk.dim(kb + "KB")} ${ok}`;
        break;
      }
      case "RX": {
        const name = e.name.length > 22 ? e.name.slice(0, 22) : e.name;
        const payload = e.payload ? chalk.dim(` ${e.payload}`) : "";
        line = `${chalk.dim(ts)} ${chalk.green("RX")} ${chalk.cyan(e.cmd)} ${chalk.white(name)}${payload}`;
        break;
      }
      case "WIFI":
        line = `${chalk.dim(ts)} ${chalk.yellow("WIFI")} ${chalk.yellow(e.event)} state=${e.state}`;
        break;
      case "STATE":
        line = `${chalk.dim(ts)} ${chalk.magenta("STATE")} ph=${e.phase} wph=${e.wifi_phase} wifi=${e.wifi_active} tcp=${e.tcp_connected}`;
        break;
      case "COMPRESS":
        line = `${chalk.dim(ts)} ${chalk.dim("COMPRESS")} ${e.raw}→${e.compressed}B (${(e.ratio * 100).toFixed(1)}%) ${e.ms}ms`;
        break;
      case "LOG": {
        const lvlColor = e.level === "ERROR" ? chalk.red : e.level === "WARN" ? chalk.yellow : chalk.dim;
        line = `${chalk.dim(ts)} ${lvlColor(e.level)} ${chalk.dim(e.msg.slice(0, 60))}`;
        break;
      }
      default:
        line = `${chalk.dim(ts)} ${chalk.dim(JSON.stringify(e).slice(0, 60))}`;
    }

    this.entries.push(line);
    if (this.entries.length > MAX_ENTRIES) {
      this.entries.shift();
    }
  }

  scroll(delta: number) {
    this.scrollOffset = Math.max(0, this.scrollOffset + delta);
  }

  render(width: number): string[] {
    const lines: string[] = [chalk.bold(" EVENT LOG")];
    const availHeight = 20; // approximate
    const visEntries = this.entries.slice(-(availHeight + this.scrollOffset), this.scrollOffset > 0 ? -this.scrollOffset : undefined);
    for (const entry of visEntries) {
      lines.push(" " + entry.slice(0, width - 2));
    }
    return lines;
  }
}
