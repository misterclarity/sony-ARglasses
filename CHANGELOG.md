# Changelog

## [0.1.0] — 2026-06-06

First release.

### Features
- BT RFCOMM connection + full handshake (ProtocolVersion → SettingsStatus → Version → NewHostApp → SyncResponse)
- Display rendering: 419×138 8-bit grayscale, raw DEFLATE (`wbits=-15`), ~0.2% compression ratio
- Glider animation demo (~2.5fps over BT)
- WiFi data path: same-network mode — both devices join an existing AP, no Internet Sharing needed
- Auto-discovery: scans paired SmartEyeglass devices, picks on connect
- JSON event log at `/tmp/glasses-events.jsonl` (TX, RX, STATE, WIFI, COMPRESS, LOG)
- TypeScript TUI harness with live event log, protocol state panel, guardrail mode
- pytest test suite for BT handshake, WiFi flow, and display rendering
- `glasses-wifi-setup.sh` — WiFi readiness checker (en0 IP + `.env` validation)
- 3-pane tmux session (`harness.tmux.sh`)
