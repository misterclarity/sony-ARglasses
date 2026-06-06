#!/usr/bin/env node
import { TUI, Container, Box, Text, Spacer, ProcessTerminal, type Component } from "@earendil-works/pi-tui";
import chalk from "chalk";

import { tailEvents } from "./events.js";
import { makeInitialState, applyEvent } from "./state.js";
import { GlassesProcess } from "./process.js";
import { HeaderBar } from "./components/HeaderBar.js";
import { EventLog } from "./components/EventLog.js";
import { ProtocolStatePanel } from "./components/ProtocolState.js";
import { QuickActions } from "./components/QuickActions.js";
import { MessageComposer } from "./components/MessageComposer.js";
import { GuardrailPanel } from "./components/GuardrailPanel.js";

const EVENTS_PATH = "/tmp/glasses-events.jsonl";
const args = process.argv.slice(2);
const noGlasses = args.includes("--no-glasses");

// ── State ─────────────────────────────────────────────────────────────────────
let protocolState = makeInitialState();
let guardrailOn = false;

// ── Process ───────────────────────────────────────────────────────────────────
const glassesProc = new GlassesProcess();

function sendCmd(raw: string) {
  if (noGlasses) {
    eventLog.addEvent({ ts: Date.now(), type: "LOG", level: "INFO", msg: `[no-glasses] ${raw}` });
    tui.requestRender();
    return;
  }
  glassesProc.send(raw);
}

function handleUserCommand(raw: string) {
  const cmd = raw.trim();
  if (!cmd) return;

  if (guardrailOn) {
    guardrailPanel.setPending(cmd);
    tui.setFocus(guardrailPanel as unknown as Component);
    tui.requestRender();
    return;
  }
  sendCmd(cmd);
}

// ── TUI Components ────────────────────────────────────────────────────────────
const headerBar = new HeaderBar(protocolState, guardrailOn);
const eventLog = new EventLog();
const statePanel = new ProtocolStatePanel(protocolState);
const quickActions = new QuickActions();
const composer = new MessageComposer();
const guardrailPanel = new GuardrailPanel();

// ── Split panel (event log left, protocol state right) ─────────────────────
class SplitPanel implements Component {
  invalidate() {}
  render(width: number): string[] {
    const leftW = Math.floor(width * 0.55);
    const rightW = width - leftW - 1;
    const leftLines = eventLog.render(leftW);
    const rightLines = statePanel.render(rightW);
    const maxLen = Math.max(leftLines.length, rightLines.length);
    const out: string[] = [];
    for (let i = 0; i < maxLen; i++) {
      const raw = leftLines[i] ?? "";
      // Pad to leftW accounting for ANSI escape sequences
      const visible = raw.replace(/\x1b\[[0-9;]*m/g, "");
      const pad = " ".repeat(Math.max(0, leftW - visible.length));
      const r = rightLines[i] ?? "";
      out.push(raw + pad + chalk.dim("│") + r);
    }
    return out;
  }
}

const splitPanel = new SplitPanel();

// ── Wire up TUI ───────────────────────────────────────────────────────────────
const terminal = new ProcessTerminal();
const tui = new TUI(terminal);

// Build layout
tui.addChild(headerBar);
tui.addChild(new Text(chalk.dim("─".repeat(120))));
tui.addChild(splitPanel);
tui.addChild(new Text(chalk.dim("─".repeat(120))));
tui.addChild(quickActions);
tui.addChild(guardrailPanel);
tui.addChild(composer);

tui.setFocus(composer);

composer.onSubmit = (val) => {
  composer.setValue("");
  handleUserCommand(val);
  tui.requestRender();
};

composer.onEscape = () => {
  composer.setValue("");
  tui.requestRender();
};

// Keyboard shortcuts
tui.addInputListener((data) => {
  // If guardrail panel is waiting, let it handle A/S
  if (guardrailPanel.hasPending()) {
    guardrailPanel.handleInput(data);
    tui.requestRender();
    return { consume: true };
  }

  const keyMap: Record<string, string> = {
    w: "wifi on",
    c: "wifi connect auto",
    s: "wifi switch",
    b: "wifi bt",
    g: "glider",
    x: "stop",
    "?": "help",
  };

  if (data === "G") {
    guardrailOn = !guardrailOn;
    headerBar.update(protocolState, guardrailOn);
    tui.requestRender();
    return { consume: true };
  }

  if (data === "q") {
    sendCmd("quit");
    setTimeout(() => { glassesProc.stop(); tui.stop(); process.exit(0); }, 600);
    return { consume: true };
  }

  if (keyMap[data]) {
    handleUserCommand(keyMap[data]);
    return { consume: true };
  }

  return undefined;
});

// Guardrail panel callbacks
guardrailPanel.onAllow = (cmd) => {
  tui.setFocus(composer);
  sendCmd(cmd);
  tui.requestRender();
};
guardrailPanel.onSkip = () => {
  tui.setFocus(composer);
  tui.requestRender();
};

// ── Event tailing ─────────────────────────────────────────────────────────────
tailEvents(EVENTS_PATH, (e) => {
  protocolState = applyEvent(protocolState, e);
  eventLog.addEvent(e);
  headerBar.update(protocolState, guardrailOn);
  statePanel.update(protocolState);
  tui.requestRender();
});

// ── Start process ─────────────────────────────────────────────────────────────
if (noGlasses) {
  // Seed a demo event so the log isn't empty
  eventLog.addEvent({ ts: Date.now(), type: "LOG", level: "INFO", msg: "[no-glasses] dev mode — TUI only" });
} else {
  glassesProc.onStdout = (line) => {
    eventLog.addEvent({ ts: Date.now(), type: "LOG", level: "INFO", msg: line.slice(0, 120) });
    tui.requestRender();
  };
  glassesProc.onStderr = (line) => {
    eventLog.addEvent({ ts: Date.now(), type: "LOG", level: "WARN", msg: line.slice(0, 120) });
    tui.requestRender();
  };
  glassesProc.onExit = (code) => {
    eventLog.addEvent({ ts: Date.now(), type: "LOG", level: "WARN", msg: `glasses-tool exited (code=${code})` });
    tui.requestRender();
  };
  glassesProc.start(false);
}

tui.start();
tui.requestRender();
