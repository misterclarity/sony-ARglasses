import { Input } from "@earendil-works/pi-tui";
import chalk from "chalk";

export class MessageComposer extends Input {
  render(width: number): string[] {
    const base = super.render(width);
    const enterHint = chalk.dim(" [Enter]");
    if (base.length > 0) {
      const line = base[0];
      const paddedWidth = width - enterHint.replace(/\x1b\[[0-9;]*m/g, "").length;
      base[0] = line.slice(0, paddedWidth) + enterHint;
    }
    return base;
  }
}
