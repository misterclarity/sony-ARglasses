#!/usr/bin/env python3
"""
dashboard.py - tkinter control panel for the Sony SED-E1 Windows driver.

One-button connect (Bluetooth -> hotspot -> WiFi) plus microphone record/playback
and a live colour log. Built on tkinter (stdlib). Two optional packages enable the
hotspot automation and audio:  pip install -r requirements-dashboard.txt
(winsdk, sounddevice, numpy). Without them the GUI still runs; those features
degrade gracefully.

Run:  python dashboard.py

Connect button does, in order:
  1. Turn the Mobile Hotspot OFF (a fresh BT link can't form while it runs).
  2. Connect Bluetooth + run the handshake (retrying until the glasses appear).
     -> Confirm the alignment screen on the glasses (controller button).
  3. Turn the Mobile Hotspot ON (2.4 GHz, SSID/pass from Windows hotspot config).
  4. Run the WiFi handoff (wifi on -> connect -> switch) -> 30fps glider over WiFi.
"""
import os
import queue
import threading
import time
import wave
import tkinter as tk
from tkinter import scrolledtext

import glasses_tool as g

# Optional deps - imported lazily so the GUI still opens without them.
try:
    import asyncio
    from winsdk.windows.networking.connectivity import NetworkInformation
    from winsdk.windows.networking.networkoperators import NetworkOperatorTetheringManager
    _HAS_TETHER = True
except Exception:
    _HAS_TETHER = False

try:
    import numpy as np
    import sounddevice as sd
    _HAS_AUDIO = True
except Exception:
    _HAS_AUDIO = False

COLOR_MAP = {
    g.CLR_RED: "#ff5555", g.CLR_GRN: "#50fa7b", g.CLR_YLW: "#f1fa8c",
    g.CLR_BLU: "#6cb6ff", g.CLR_MAG: "#ff79c6", g.CLR_CYN: "#8be9fd",
    g.CLR_RST: "#d0d0d0",
}
BG = "#1e1f29"
PANEL = "#282a36"
MIC_WAV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "glasses-mic.wav")


# ---------------------------------------------------------------------------
# Mobile Hotspot control (winsdk NetworkOperatorTetheringManager)
# ---------------------------------------------------------------------------
def _tether_mgr():
    prof = NetworkInformation.get_internet_connection_profile()
    if prof is None:
        raise RuntimeError("no internet connection profile to share")
    return NetworkOperatorTetheringManager.create_from_connection_profile(prof)


def hotspot_on():
    if not _HAS_TETHER:
        return False
    try:
        m = _tether_mgr()
        if m.tethering_operational_state == 1:
            return True
        asyncio.run(_await(m.start_tethering_async()))
        return True
    except Exception as exc:
        g.log(f"hotspot on failed: {exc}", g.CLR_RED)
        return False


def hotspot_off():
    if not _HAS_TETHER:
        return False
    try:
        m = _tether_mgr()
        if m.tethering_operational_state == 2:
            return True
        asyncio.run(_await(m.stop_tethering_async()))
        return True
    except Exception as exc:
        g.log(f"hotspot off failed: {exc}", g.CLR_YLW)
        return False


async def _await(op):
    return await op


# ---------------------------------------------------------------------------
# Microphone (sounddevice) - the glasses' HFP headset input
# ---------------------------------------------------------------------------
def find_glasses_mic():
    if not _HAS_AUDIO:
        return None, None
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0 and "eyeglass" in d["name"].lower():
            return i, d
    return None, None


class Dashboard:
    def __init__(self, root):
        self.root = root
        self.cfg = g.Config.load()
        path = os.environ.get("GLASSES_EVENTS",
                              os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                           "glasses-events.jsonl"))
        self.jlog = g.JSONEventLog(path)

        self.session = None
        self.transport = None
        self._connecting = False
        self._busy_audio = False
        self._stop_connect = threading.Event()
        self._last_audio = None        # (np.ndarray, samplerate)
        self.mic_max_gain = 25.0       # cap for digital normalization of the quiet HFP mic
        self.logq = queue.Queue()
        g.add_log_sink(lambda line, color: self.logq.put((line, color)))

        self._build_ui()
        self.root.after(80, self._drain_log)
        self.root.after(300, self._update_status)
        if not _HAS_TETHER:
            g.log("winsdk not installed - hotspot won't auto-toggle "
                  "(pip install -r requirements-dashboard.txt).", g.CLR_YLW)
        if not _HAS_AUDIO:
            g.log("sounddevice not installed - mic record/play disabled.", g.CLR_YLW)

    # -- UI ------------------------------------------------------------------
    def _build_ui(self):
        self.root.title("Sony SED-E1 - Glasses Dashboard")
        self.root.configure(bg=BG)
        self.root.geometry("860x640")

        top = tk.Frame(self.root, bg=PANEL)
        top.pack(fill="x", padx=8, pady=(8, 4))
        self.status_var = tk.StringVar(value="Disconnected")
        tk.Label(top, text="Status:", bg=PANEL, fg="#8be9fd",
                 font=("Segoe UI", 10, "bold")).pack(side="left", padx=(8, 4), pady=6)
        tk.Label(top, textvariable=self.status_var, bg=PANEL, fg="#f8f8f2",
                 font=("Segoe UI", 10)).pack(side="left", pady=6)
        self.hotspot_var = tk.StringVar(value="Hotspot: ?")
        tk.Label(top, textvariable=self.hotspot_var, bg=PANEL, fg="#f1fa8c",
                 font=("Segoe UI", 10)).pack(side="right", padx=10, pady=6)

        btns = tk.Frame(self.root, bg=BG)
        btns.pack(fill="x", padx=8, pady=4)
        self.btn_connect = self._mkbtn(btns, "Connect  (BT -> WiFi)", self.on_connect, "#50fa7b")
        self.btn_glider = self._mkbtn(btns, "Glider", self.on_glider, "#bd93f9")
        self.btn_stop = self._mkbtn(btns, "Stop", self.on_stop, "#ffb86c")
        self.btn_disc = self._mkbtn(btns, "Disconnect", self.on_disconnect, "#ff5555")

        btns2 = tk.Frame(self.root, bg=BG)
        btns2.pack(fill="x", padx=8, pady=(0, 4))
        self.btn_rec = self._mkbtn(btns2, "Record mic (5s)", self.on_record, "#8be9fd")
        self.btn_play = self._mkbtn(btns2, "Play back", self.on_play, "#8be9fd")

        self.log = scrolledtext.ScrolledText(
            self.root, bg="#11121a", fg="#d0d0d0", insertbackground="#d0d0d0",
            font=("Consolas", 9), wrap="word", state="disabled", height=24)
        self.log.pack(fill="both", expand=True, padx=8, pady=(4, 8))
        for hexcol in set(COLOR_MAP.values()):
            self.log.tag_configure(hexcol, foreground=hexcol)
        self._refresh_buttons()

    def _mkbtn(self, parent, text, cmd, color):
        b = tk.Button(parent, text=text, command=cmd, bg=PANEL, fg=color,
                      activebackground="#44475a", activeforeground=color,
                      font=("Segoe UI", 10, "bold"), relief="flat", padx=10, pady=6,
                      disabledforeground="#555")
        b.pack(side="left", padx=4)
        return b

    def _append(self, line, color):
        hexcol = COLOR_MAP.get(color, "#d0d0d0")
        self.log.configure(state="normal")
        self.log.insert("end", line + "\n", hexcol)
        if int(self.log.index("end-1c").split(".")[0]) > 1500:
            self.log.delete("1.0", "300.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _drain_log(self):
        try:
            while True:
                line, color = self.logq.get_nowait()
                self._append(line, color)
        except queue.Empty:
            pass
        self.root.after(80, self._drain_log)

    # -- status / button state ----------------------------------------------
    def _update_status(self):
        s = self.session
        if self._connecting:
            pass  # status set by the worker via the log; keep last line
        elif s is None:
            self.status_var.set("Disconnected")
        else:
            phase = s.init_phase
            wifi = "WiFi ACTIVE (30fps)" if s.wifi_active else f"wifi_phase={s.wifi_phase}"
            pname = {0: "init", 5: "ready"}.get(phase, f"phase {phase}")
            tail = " - TAP alignment on glasses" if phase == 4 else ""
            self.status_var.set(f"BT connected | {pname} | {wifi}{tail}")
        if _HAS_TETHER:
            ip = g.get_hotspot_ip()
            self.hotspot_var.set(f"Hotspot: {ip}" if ip else "Hotspot: OFF")
        else:
            self.hotspot_var.set("Hotspot: (winsdk missing)")
        self._refresh_buttons()
        self.root.after(300, self._update_status)

    def _refresh_buttons(self):
        s = self.session
        connected = s is not None
        ready = connected and s.init_phase == 5
        self._set(self.btn_connect, not connected and not self._connecting)
        self._set(self.btn_glider, ready)
        self._set(self.btn_stop, ready)
        self._set(self.btn_disc, connected)
        self._set(self.btn_rec, _HAS_AUDIO and not self._busy_audio)
        self._set(self.btn_play, _HAS_AUDIO and self._last_audio is not None
                  and not self._busy_audio)

    @staticmethod
    def _set(btn, enabled):
        btn.configure(state="normal" if enabled else "disabled")

    def _set_status(self, text):
        self.status_var.set(text)

    # -- one-button connect --------------------------------------------------
    def on_connect(self):
        if self.session or self._connecting:
            return
        self._connecting = True
        self._stop_connect.clear()
        self._refresh_buttons()
        threading.Thread(target=self._full_connect_worker, daemon=True).start()

    def _full_connect_worker(self):
        try:
            # 1. Hotspot OFF so the BT link can form on the shared radio.
            if _HAS_TETHER and g.get_hotspot_ip():
                g.log("Turning hotspot OFF for the Bluetooth connect...", g.CLR_CYN)
                hotspot_off()
                time.sleep(1.5)

            # 2. Connect Bluetooth + handshake (retry until glasses appear).
            mac = g.resolve_address(self.cfg)
            if not mac:
                g.log("No glasses found. Pair them first.", g.CLR_RED)
                return
            channel = self.cfg.rfcomm_channel
            pretty = ":".join(mac[i:i + 2] for i in range(0, 12, 2))
            self.root.after(0, self._set_status,
                            "Connecting Bluetooth - power-cycle glasses to 'Waiting to connect'")
            g.log(f"Connecting to {pretty} ch{channel} - power-cycle glasses now...", g.CLR_CYN)
            deadline = time.time() + 60
            transport = None
            attempt = 0
            while transport is None and not self._stop_connect.is_set():
                attempt += 1
                try:
                    transport = g.BTRFCOMMTransport(mac, channel)
                except Exception as exc:
                    if time.time() >= deadline:
                        g.log(f"Gave up after {attempt} attempts: {exc}", g.CLR_RED)
                        return
                    g.log(f"  attempt {attempt} failed; retrying...", g.CLR_YLW)
                    time.sleep(1.0)
            if transport is None:
                return
            self.transport = transport
            self.session = g.Session(transport, self.cfg, self.jlog)
            threading.Thread(target=self.session.read_loop, daemon=True).start()
            g.log("Bluetooth connected. Confirm the ALIGNMENT screen on the glasses "
                  "(controller button)...", g.CLR_GRN)

            # 3. Wait for the physical alignment tap -> phase 5.
            wait_until = time.time() + 120
            while self.session and self.session.init_phase != 5:
                if self._stop_connect.is_set() or time.time() > wait_until:
                    g.log("Did not reach phase 5 (alignment not confirmed?). "
                          "WiFi step skipped.", g.CLR_YLW)
                    return
                time.sleep(0.3)

            # 4. Hotspot ON, then run the WiFi handoff.
            if _HAS_TETHER:
                g.log("Phase 5 reached. Turning hotspot ON (2.4 GHz)...", g.CLR_CYN)
                if hotspot_on():
                    for _ in range(20):
                        if g.get_hotspot_ip():
                            break
                        time.sleep(0.5)
            ip = g.get_hotspot_ip()
            if not ip:
                g.log("Hotspot not up - turn on Mobile Hotspot manually, then it will "
                      "proceed.", g.CLR_YLW)
                for _ in range(40):
                    if g.get_hotspot_ip():
                        break
                    time.sleep(0.5)
            if not g.get_hotspot_ip():
                g.log("No hotspot; staying on Bluetooth (BT glider available).", g.CLR_YLW)
                return
            g.log("Starting WiFi handoff: wifi on -> connect -> switch ...", g.CLR_GRN)
            self.session.auto_wifi = True
            self.session.send_cmd([0x92, 0x00, 0x00], "WifiTurnOnReq")
            self.session.wifi_phase = 10
        finally:
            self._connecting = False

    # -- display -------------------------------------------------------------
    def on_glider(self):
        if self.session and self.session.init_phase == 5:
            self.session.start_glider(fps=30 if self.session.wifi_active else 2.5)

    def on_stop(self):
        if self.session:
            self.session.stop_display()
            self.session.send_cmd(g.build_layout_cmd(g.pattern_black(), self.jlog),
                                  "STOP - black frame")

    def on_disconnect(self):
        self._stop_connect.set()
        s = self.session
        self.session = None
        if s:
            threading.Thread(target=s.shutdown, daemon=True).start()
        if _HAS_TETHER and g.get_hotspot_ip():
            threading.Thread(target=hotspot_off, daemon=True).start()
        g.log("Disconnected.", g.CLR_YLW)

    # -- microphone ----------------------------------------------------------
    def on_record(self):
        if self._busy_audio or not _HAS_AUDIO:
            return
        self._busy_audio = True
        self._refresh_buttons()
        threading.Thread(target=self._record_worker, daemon=True).start()

    def _record_worker(self):
        try:
            idx, dev = find_glasses_mic()
            if idx is None:
                g.log("Glasses mic not found as a recording device. Connect the "
                      "SmartEyeglass Hands-Free audio in Windows Bluetooth first.", g.CLR_RED)
                return
            fs = int(dev.get("default_samplerate") or 16000)
            secs = 5
            g.log(f"Recording 5s from '{dev['name'].splitlines()[0]}' @ {fs}Hz...", g.CLR_CYN)
            audio = sd.rec(int(secs * fs), samplerate=fs, channels=1,
                           device=idx, dtype="int16")
            sd.wait()

            # HFP mic captures at a low level - normalize digitally toward full
            # scale (capped, so we don't blow up pure silence/hiss).
            raw_peak = int(np.abs(audio).max())
            gain = 1.0
            if 0 < raw_peak < int(0.9 * 32767):
                gain = min(0.9 * 32767 / raw_peak, self.mic_max_gain)
                audio = np.clip(audio.astype(np.float32) * gain,
                                -32768, 32767).astype(np.int16)
            new_peak = int(np.abs(audio).max())

            self._last_audio = (audio, fs)
            with wave.open(MIC_WAV, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(fs)
                w.writeframes(audio.tobytes())
            if raw_peak < 150:
                g.log(f"Recorded 5s, but raw peak {raw_peak} is near silence - "
                      "is the Hands-Free mic actually connected/streaming?", g.CLR_YLW)
            else:
                g.log(f"Recorded 5s -> {MIC_WAV} (raw peak {raw_peak}, +{gain:.1f}x "
                      f"gain -> {new_peak}/32767). Press Play back.", g.CLR_GRN)
        except Exception as exc:
            g.log(f"Record failed: {exc}", g.CLR_RED)
        finally:
            self._busy_audio = False

    def on_play(self):
        if self._busy_audio or not _HAS_AUDIO or self._last_audio is None:
            return
        self._busy_audio = True
        self._refresh_buttons()
        threading.Thread(target=self._play_worker, daemon=True).start()

    def _play_worker(self):
        try:
            audio, fs = self._last_audio
            g.log("Playing back recording on the default speakers...", g.CLR_CYN)
            sd.play(audio, fs)        # default output device (PC speakers)
            sd.wait()
            g.log("Playback done.", g.CLR_GRN)
        except Exception as exc:
            g.log(f"Playback failed: {exc}", g.CLR_RED)
        finally:
            self._busy_audio = False


def main():
    root = tk.Tk()
    Dashboard(root)
    root.mainloop()


if __name__ == "__main__":
    main()
