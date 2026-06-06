import type { Component } from "@earendil-works/pi-tui";
import chalk from "chalk";

export class GuardrailPanel implements Component {
  private pending: string | null = null;
  public onAllow?: (cmd: string) => void;
  public onSkip?: () => void;

  setPending(cmd: string) {
    this.pending = cmd;
  }

  clear() {
    this.pending = null;
  }

  hasPending() {
    return this.pending !== null;
  }

  handleInput(data: string) {
    if (!this.pending) return;
    if (data === "a" || data === "A") {
      const cmd = this.pending;
      this.pending = null;
      this.onAllow?.(cmd);
    } else if (data === "s" || data === "S") {
      this.pending = null;
      this.onSkip?.();
    }
  }

  invalidate() {}

  render(width: number): string[] {
    if (!this.pending) return [];
    return [
      chalk.bgYellow.black(" ⚠ GUARDRAIL ") +
        chalk.yellow(` PENDING: ${this.pending}`) +
        chalk.dim("  [A]llow  [S]kip"),
    ];
  }
}
