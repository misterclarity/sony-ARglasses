import type { Component } from "@earendil-works/pi-tui";
import chalk from "chalk";

const SHORTCUTS: Array<[string, string]> = [
  ["w", "wifi on"],
  ["c", "connect"],
  ["s", "switch"],
  ["b", "bt back"],
  ["g", "glider"],
  ["x", "stop"],
  ["G", "guardrail"],
  ["?", "help"],
  ["q", "quit"],
];

export class QuickActions implements Component {
  invalidate() {}

  render(width: number): string[] {
    const parts = SHORTCUTS.map(
      ([key, desc]) => `${chalk.yellow("[" + key + "]")}${chalk.dim(desc)}`
    );
    const line = " " + parts.join("  ");
    const divider = chalk.dim("─".repeat(width));
    return [divider, line];
  }
}
