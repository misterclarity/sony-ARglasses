#!/bin/bash
# glasses-wifi-setup.sh — Sony SED-E1 WiFi readiness check
#
# Verifies that Mac and glasses are configured to use the same WiFi network.
# Both devices must be on the same AP — no Internet Sharing or hotspot needed.
#
# Usage: ./glasses-wifi-setup.sh [check|env]

set -euo pipefail
REPO="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="$REPO/macos-middleware/.env"

case "${1:-check}" in
check)
    echo "═══ SED-E1 WiFi readiness check ═══"
    echo ""

    # 1. en0 IP
    EN0_IP=$(ipconfig getifaddr en0 2>/dev/null || true)
    if [ -n "$EN0_IP" ]; then
        echo "✅ Mac WiFi IP (en0):   $EN0_IP"
    else
        echo "❌ en0 has no IP — connect Mac to WiFi first"
        exit 1
    fi

    # 2. .env creds
    if [ -f "$ENV_FILE" ]; then
        SSID=$(grep '^SSID=' "$ENV_FILE" | cut -d= -f2-)
        PSWD=$(grep '^PSWD=' "$ENV_FILE" | cut -d= -f2-)
        if [ -n "$SSID" ] && [ -n "$PSWD" ]; then
            echo "✅ .env credentials:   SSID=$SSID  PSWD=***"
        else
            echo "⚠️  .env exists but SSID or PSWD is empty"
        fi
    else
        echo "❌ macos-middleware/.env not found"
        echo "   Create it:"
        echo "   echo 'SSID=YourNetwork' > macos-middleware/.env"
        echo "   echo 'PSWD=YourPassword' >> macos-middleware/.env"
        exit 1
    fi

    # 3. Connected SSID
    CURRENT_SSID=$(networksetup -getairportnetwork en0 2>/dev/null | sed 's/Current Wi-Fi Network: //' || true)
    if [ -n "$CURRENT_SSID" ]; then
        echo "✅ Mac connected to:   $CURRENT_SSID"
        if [ -n "$SSID" ] && [ "$CURRENT_SSID" != "$SSID" ]; then
            echo "⚠️  Mismatch — .env says '$SSID' but Mac is on '$CURRENT_SSID'"
            echo "   Either update .env or switch Mac to the '$SSID' network"
        fi
    else
        echo "⚠️  Could not determine current SSID (networksetup)"
    fi

    echo ""
    echo "When glasses are powered on and you're in the REPL:"
    echo "  wifi on"
    echo "  wifi connect auto"
    echo "  wifi switch"
    ;;

env)
    # Print current .env (mask password)
    if [ -f "$ENV_FILE" ]; then
        echo "=== $ENV_FILE ==="
        sed 's/\(PSWD=\).*/\1***/' "$ENV_FILE"
    else
        echo "No .env at $ENV_FILE"
    fi
    ;;

*)
    echo "Usage: $0 [check|env]"
    echo "  check  — verify WiFi readiness (default)"
    echo "  env    — show current .env (passwords masked)"
    ;;
esac
