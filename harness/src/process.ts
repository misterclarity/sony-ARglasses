import { spawn, type ChildProcessWithoutNullStreams } from "child_process";
import { resolve } from "path";
import { fileURLToPath } from "url";
import { dirname } from "path";

const __dirname = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = resolve(__dirname, "../../");
const GLASSES_TOOL = resolve(REPO_ROOT, "macos-middleware/glasses-tool");

export class GlassesProcess {
  private proc: ChildProcessWithoutNullStreams | null = null;
  public onStdout?: (line: string) => void;
  public onStderr?: (line: string) => void;
  public onExit?: (code: number | null) => void;

  start(noGlasses = false) {
    if (noGlasses) return; // dev mode — no subprocess
    this.proc = spawn(GLASSES_TOOL, ["connect"], {
      stdio: ["pipe", "pipe", "pipe"],
      cwd: REPO_ROOT,
    });

    let stdoutBuf = "";
    this.proc.stdout.on("data", (chunk: Buffer) => {
      stdoutBuf += chunk.toString();
      const lines = stdoutBuf.split("\n");
      stdoutBuf = lines.pop() ?? "";
      for (const line of lines) {
        this.onStdout?.(line);
      }
    });

    let stderrBuf = "";
    this.proc.stderr.on("data", (chunk: Buffer) => {
      stderrBuf += chunk.toString();
      const lines = stderrBuf.split("\n");
      stderrBuf = lines.pop() ?? "";
      for (const line of lines) {
        this.onStderr?.(line);
      }
    });

    this.proc.on("exit", (code) => {
      this.onExit?.(code);
      this.proc = null;
    });
  }

  send(cmd: string) {
    if (!this.proc?.stdin.writable) return;
    this.proc.stdin.write(cmd + "\n");
  }

  stop() {
    if (this.proc) {
      try { this.proc.stdin.write("quit\n"); } catch {}
      setTimeout(() => this.proc?.kill(), 500);
    }
  }

  isRunning() {
    return this.proc !== null;
  }
}
