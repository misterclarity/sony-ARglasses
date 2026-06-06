import { createReadStream, watchFile } from "fs";
import { existsSync } from "fs";
import readline from "readline";
import { EventEmitter } from "events";

export type GlassesEventType = "TX" | "RX" | "STATE" | "LOG" | "WIFI" | "COMPRESS";

export interface TXEvent {
  ts: number; type: "TX";
  cmd: string; name: string; bytes: number;
  phase: number; wifi_active: boolean; ok: boolean;
}
export interface RXEvent {
  ts: number; type: "RX";
  cmd: string; name: string; payload: string; phase: number;
}
export interface StateEvent {
  ts: number; type: "STATE";
  phase: number; wifi_phase: number; wifi_active: boolean;
  tcp_connected: boolean; bt_connected: boolean;
}
export interface LogEvent {
  ts: number; type: "LOG";
  level: "INFO" | "WARN" | "ERROR"; msg: string;
}
export interface WifiEvent {
  ts: number; type: "WIFI";
  event: "ENABLED" | "CONNECTED" | "SWITCHED" | "DROPPED"; state: number;
}
export interface CompressEvent {
  ts: number; type: "COMPRESS";
  raw: number; compressed: number; ratio: number; ms: number;
}

export type GlassesEvent = TXEvent | RXEvent | StateEvent | LogEvent | WifiEvent | CompressEvent;

export class EventTailer extends EventEmitter {
  private offset = 0;
  private watching = false;

  constructor(private path: string) {
    super();
  }

  start() {
    if (this.watching) return;
    this.watching = true;
    if (existsSync(this.path)) {
      this.readNew();
    }
    watchFile(this.path, { interval: 100 }, () => this.readNew());
  }

  stop() {
    // watchFile is process-scoped; just mark stopped
    this.watching = false;
  }

  private readNew() {
    if (!existsSync(this.path)) return;
    const stream = createReadStream(this.path, { start: this.offset, encoding: "utf8" });
    const rl = readline.createInterface({ input: stream, crlfDelay: Infinity });
    const lines: string[] = [];
    rl.on("line", (line) => {
      if (!line.trim()) return;
      try {
        const e = JSON.parse(line) as GlassesEvent;
        this.offset += Buffer.byteLength(line + "\n");
        lines.push(line);
        this.emit("event", e);
      } catch {
        // skip malformed lines
      }
    });
  }
}

export function tailEvents(path: string, onEvent: (e: GlassesEvent) => void): () => void {
  const tailer = new EventTailer(path);
  tailer.on("event", onEvent);
  tailer.start();
  return () => tailer.stop();
}
