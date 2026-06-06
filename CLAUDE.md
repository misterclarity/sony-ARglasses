# CLAUDE.md — sony-sed-e1

## Project
Sony SED-E1 SmartEyeglass macOS middleware, TUI harness, and test suite.

## External Monitor Integration

An external session monitor analyzes your work in real-time and delivers proposals.

**Between tasks or after completing a tool-calling sequence, check:**
```
.monitor/proposal-ready.flag
```

**If the flag file exists:**
1. Read the `proposal:` path from the flag to find the proposal markdown
2. Briefly summarize the proposal to the user: "📡 Monitor proposal ready — [one-line summary]. Review?"
3. Do NOT auto-act on the proposal. Wait for user confirmation
4. After user reviews: delete the flag file (`rm .monitor/proposal-ready.flag`)

**If the flag does NOT exist:** continue normally, do not mention the monitor.

**Rules:**
- Never modify proposal files — they are read-only artifacts from the monitor
- Never suppress or hide proposals from the user
- If multiple proposals accumulate, present the latest first
- The monitor runs independently; its proposals reflect analysis of YOUR session logs
