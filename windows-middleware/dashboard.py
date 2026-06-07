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
import json
import queue
import threading
import time
import wave
import tkinter as tk
from tkinter import scrolledtext, filedialog

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

try:
    import io
    from PIL import Image, ImageTk, ImageDraw, ImageFont, ImageOps
    _HAS_IMG = True
except Exception:
    _HAS_IMG = False

try:
    from vosk import Model as VoskModel, KaldiRecognizer
    _HAS_VOSK = True
except Exception:
    _HAS_VOSK = False

VOSK_MODEL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "models", "vosk-model-small-en-us-0.15")
VOICE_WORDS = ("photo", "picture")
VOICE_GRAMMAR = '["photo", "picture", "[unk]"]'

COLOR_MAP = {
    g.CLR_RED: "#ff5555", g.CLR_GRN: "#50fa7b", g.CLR_YLW: "#f1fa8c",
    g.CLR_BLU: "#6cb6ff", g.CLR_MAG: "#ff79c6", g.CLR_CYN: "#8be9fd",
    g.CLR_RST: "#d0d0d0",
}
BG = "#1e1f29"
PANEL = "#282a36"
MIC_WAV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "glasses-mic.wav")
PHOTO_JPG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "glasses-photo.jpg")


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
    """Find the glasses' Hands-Free input. Windows lists each device under
    several host APIs; prefer MME (supports blocking streams) over WDM-KS."""
    if not _HAS_AUDIO:
        return None, None
    try:
        hostapis = sd.query_hostapis()
    except Exception:
        hostapis = []

    def api_name(d):
        try:
            return hostapis[d["hostapi"]]["name"]
        except Exception:
            return ""

    pref = ["MME", "Windows WASAPI", "Windows DirectSound", "Windows WDM-KS"]
    cands = [(i, d) for i, d in enumerate(sd.query_devices())
             if d["max_input_channels"] > 0 and "eyeglass" in d["name"].lower()]
    if not cands:
        return None, None
    cands.sort(key=lambda t: pref.index(api_name(t[1])) if api_name(t[1]) in pref else 99)
    return cands[0]


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
        self._busy_photo = False
        self._preview_img = None       # keep a ref so tk doesn't GC the PhotoImage
        self._voice_on = False
        self._voice_stop = threading.Event()
        self._vosk_model = None        # loaded lazily on first enable
        self._voice_cooldown = 0.0
        self.voice_gain = 12.0         # boost the quiet HFP mic for recognition
        self._coords_on = False
        self._coords_stop = threading.Event()
        self._coord_font = None
        self._coord_font_small = None
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
        self.btn_image = self._mkbtn(btns, "Show Image...", self.on_show_image, "#8be9fd")
        self.btn_stop = self._mkbtn(btns, "Stop", self.on_stop, "#ffb86c")
        self.btn_disc = self._mkbtn(btns, "Disconnect", self.on_disconnect, "#ff5555")

        btns2 = tk.Frame(self.root, bg=BG)
        btns2.pack(fill="x", padx=8, pady=(0, 4))
        self.btn_rec = self._mkbtn(btns2, "Record mic (5s)", self.on_record, "#8be9fd")
        self.btn_play = self._mkbtn(btns2, "Play back", self.on_play, "#8be9fd")
        self.btn_photo = self._mkbtn(btns2, "Take Photo", self.on_photo, "#50fa7b")
        self.btn_voice = self._mkbtn(btns2, "Voice: OFF", self.on_voice, "#bd93f9")
        self.btn_coords = self._mkbtn(btns2, "Coordinates", self.on_coordinates, "#ffb86c")
        self.btn_help = self._mkbtn(btns2, "Help", self.on_help, "#f1fa8c")

        # Bottom: log on the left, camera preview on the right.
        mid = tk.Frame(self.root, bg=BG)
        mid.pack(fill="both", expand=True, padx=8, pady=(4, 8))
        self.log = scrolledtext.ScrolledText(
            mid, bg="#11121a", fg="#d0d0d0", insertbackground="#d0d0d0",
            font=("Consolas", 9), wrap="word", state="disabled", width=64)
        self.log.pack(side="left", fill="both", expand=True)
        for hexcol in set(COLOR_MAP.values()):
            self.log.tag_configure(hexcol, foreground=hexcol)

        preview = tk.Frame(mid, bg=PANEL)
        preview.pack(side="right", fill="y", padx=(8, 0))
        tk.Label(preview, text="Camera", bg=PANEL, fg="#8be9fd",
                 font=("Segoe UI", 10, "bold")).pack(pady=(6, 2), padx=8)
        self.preview_lbl = tk.Label(preview, text="(no photo yet)", bg="#11121a",
                                    fg="#777", width=44, height=20)
        self.preview_lbl.pack(padx=8, pady=(0, 8))
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
        self._set(self.btn_image, ready and _HAS_IMG)
        self._set(self.btn_stop, ready)
        self._set(self.btn_disc, connected)
        self._set(self.btn_rec, _HAS_AUDIO and not self._busy_audio)
        self._set(self.btn_play, _HAS_AUDIO and self._last_audio is not None
                  and not self._busy_audio)
        self._set(self.btn_photo, ready and not self._busy_photo)
        self._set(self.btn_voice, _HAS_VOSK and _HAS_AUDIO)
        self.btn_voice.configure(text="Voice: LISTENING" if self._voice_on else "Voice: OFF",
                                 fg="#50fa7b" if self._voice_on else "#bd93f9")
        self._set(self.btn_coords, ready and _HAS_IMG)
        self.btn_coords.configure(text="Coordinates: ON" if self._coords_on else "Coordinates",
                                  fg="#50fa7b" if self._coords_on else "#ffb86c")

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
        self._take_over_display()
        if self.session and self.session.init_phase == 5:
            self.session.start_glider(fps=30 if self.session.wifi_active else 2.5)

    def _take_over_display(self):
        """Stop any running display loop (glider / coordinates) before drawing."""
        self._coords_stop.set()
        self._coords_on = False
        if self.session:
            self.session.stop_display()

    def on_show_image(self):
        s = self.session
        if not s or s.init_phase != 5 or not _HAS_IMG:
            return
        path = filedialog.askopenfilename(
            title="Select an image to show on the glasses",
            filetypes=[("Images", "*.png *.jpg *.jpeg *.bmp *.gif *.webp"), ("All files", "*.*")])
        if not path:
            return
        threading.Thread(target=self._show_image_worker, args=(path,), daemon=True).start()

    def _show_image_worker(self, path):
        try:
            self._take_over_display()
            s = self.session
            if not s:
                return
            im = Image.open(path).convert("L")
            ow, oh = im.size
            im = ImageOps.autocontrast(im)
            fitted = ImageOps.contain(im, (g.W, g.H))     # fit, preserve aspect
            canvas = Image.new("L", (g.W, g.H), 0)
            canvas.paste(fitted, ((g.W - fitted.width) // 2, (g.H - fitted.height) // 2))
            s.send_cmd(g.build_layout_cmd(canvas.tobytes(), self.jlog), "IMAGE frame")
            g.log(f"Image: showed {os.path.basename(path)} "
                  f"({ow}x{oh} -> {g.W}x{g.H} grayscale).", g.CLR_GRN)
            self._preview_img = ImageTk.PhotoImage(canvas)
            self.root.after(0, lambda: self.preview_lbl.configure(
                image=self._preview_img, text="", width=g.W, height=g.H))
        except Exception as exc:
            g.log(f"Image failed: {exc}", g.CLR_RED)

    def on_stop(self):
        if self.session:
            self.session.stop_display()
            self.session.send_cmd(g.build_layout_cmd(g.pattern_black(), self.jlog),
                                  "STOP - black frame")

    def on_disconnect(self):
        self._stop_connect.set()
        self._voice_stop.set()
        self._voice_on = False
        self._coords_stop.set()
        self._coords_on = False
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

    # -- camera --------------------------------------------------------------
    def on_photo(self):
        s = self.session
        if self._busy_photo or not s or s.init_phase != 5:
            return
        self._busy_photo = True
        self._refresh_buttons()
        s.stop_display()          # pause the glider so it doesn't congest the link
        # Bigger resolution over WiFi (fast); smaller over BT to keep it quick.
        resolution = 0 if s.wifi_active else 4   # 0=3M, 4=VGA
        g.log(f"Camera: taking photo ({'3M via WiFi' if s.wifi_active else 'VGA via BT'})...",
              g.CLR_CYN)
        s.capture_photo(quality=2, resolution=resolution,
                        on_done=lambda jpeg, info: self.root.after(0, self._photo_done, jpeg, info))

    def _photo_done(self, jpeg, info):
        self._busy_photo = False
        self._refresh_buttons()
        if not jpeg:
            g.log(f"Camera: no image ({info}).", g.CLR_RED)
            return
        try:
            with open(PHOTO_JPG, "wb") as f:
                f.write(jpeg)
            g.log(f"Camera: saved {PHOTO_JPG} ({len(jpeg)} bytes).", g.CLR_GRN)
        except OSError as exc:
            g.log(f"Camera: save failed: {exc}", g.CLR_RED)
        if _HAS_IMG:
            try:
                im = Image.open(io.BytesIO(jpeg))
                im.thumbnail((320, 320))
                self._preview_img = ImageTk.PhotoImage(im)
                self.preview_lbl.configure(image=self._preview_img, text="", width=320, height=240)
            except Exception as exc:
                g.log(f"Camera: preview failed: {exc}", g.CLR_YLW)
        else:
            self.preview_lbl.configure(text="(install Pillow to preview;\nsaved to glasses-photo.jpg)")
            try:
                os.startfile(PHOTO_JPG)   # open in default viewer
            except Exception:
                pass

    # -- voice trigger (offline Vosk keyword spotting on the glasses mic) -----
    def on_voice(self):
        if not (_HAS_VOSK and _HAS_AUDIO):
            return
        if self._voice_on:
            self._voice_stop.set()
            self._voice_on = False
        else:
            self._voice_on = True
            self._voice_stop.clear()
            threading.Thread(target=self._voice_worker, daemon=True).start()
        self._refresh_buttons()

    def _voice_worker(self):
        # The HFP mic is only exposed under WDM-KS, which rejects *blocking*
        # reads but accepts a callback stream (as sd.rec uses). So feed a queue
        # from the audio callback and run Vosk on the queued frames.
        stream = None
        try:
            idx, dev = find_glasses_mic()
            if idx is None:
                g.log("Voice: glasses mic not found. Connect the Hands-Free audio first.",
                      g.CLR_RED)
                return
            if self._vosk_model is None:
                g.log("Voice: loading speech model...", g.CLR_CYN)
                self._vosk_model = VoskModel(VOSK_MODEL_DIR)
            fs = 8000   # HFP narrowband native rate (mic)
            # The Vosk model needs 16 kHz, so we upsample 8k->16k before feeding.
            rec = KaldiRecognizer(self._vosk_model, 16000, VOICE_GRAMMAR)
            aq = queue.Queue()

            def _cb(indata, frames, time_info, status):
                aq.put(bytes(indata))

            stream = sd.RawInputStream(samplerate=fs, blocksize=2000, device=idx,
                                       dtype="int16", channels=1, callback=_cb)
            stream.start()
            g.log("Voice: LISTENING - say 'photo' to capture.", g.CLR_GRN)
            while not self._voice_stop.is_set():
                try:
                    buf = aq.get(timeout=0.5)
                except queue.Empty:
                    continue
                arr = np.frombuffer(buf, dtype=np.int16).astype(np.float32)
                arr = np.clip(arr * self.voice_gain, -32768, 32767)
                n = len(arr)
                arr16 = np.interp(np.linspace(0, n - 1, n * 2),
                                  np.arange(n), arr).astype(np.int16)
                if rec.AcceptWaveform(arr16.tobytes()):
                    heard = json.loads(rec.Result()).get("text", "")
                else:
                    heard = json.loads(rec.PartialResult()).get("partial", "")
                if any(w in heard for w in VOICE_WORDS):
                    now = time.time()
                    if now > self._voice_cooldown and not self._busy_photo \
                            and self.session and self.session.init_phase == 5:
                        self._voice_cooldown = now + 5.0
                        g.log(f"Voice: heard '{heard}' -> taking photo!", g.CLR_MAG)
                        self.root.after(0, self.on_photo)
                        rec = KaldiRecognizer(self._vosk_model, 16000, VOICE_GRAMMAR)
        except Exception as exc:
            g.log(f"Voice error: {exc}", g.CLR_RED)
            if "not connected" in str(exc).lower() or "048F" in str(exc).upper() \
                    or "9999" in str(exc):
                g.log("  -> The glasses' Hands-Free audio isn't active. Click 'Record mic (5s)' "
                      "once to wake it, or connect SmartEyeglass audio in Windows Sound, then retry.",
                      g.CLR_YLW)
        finally:
            if stream is not None:
                try:
                    stream.stop()
                    stream.close()
                except Exception:
                    pass
            self._voice_on = False
            self.root.after(0, self._refresh_buttons)
            g.log("Voice: stopped.", g.CLR_YLW)

    # -- coordinates (render motion-sensor values on the AR display) ----------
    def _load_coord_fonts(self):
        if self._coord_font is not None:
            return
        for path in (r"C:\Windows\Fonts\consolab.ttf", r"C:\Windows\Fonts\arialbd.ttf"):
            try:
                self._coord_font = ImageFont.truetype(path, 30)
                self._coord_font_small = ImageFont.truetype(path, 18)
                return
            except Exception:
                continue
        self._coord_font = ImageFont.load_default()
        self._coord_font_small = self._coord_font

    def on_coordinates(self):
        s = self.session
        if not s or s.init_phase != 5 or not _HAS_IMG:
            return
        if self._coords_on:
            self._coords_stop.set()
            self._coords_on = False
            self._refresh_buttons()
            return
        self._coords_on = True
        self._coords_stop.clear()
        s.stop_display()                     # take over the display from the glider
        s.sensors.clear()
        g.log("Coordinates: starting accelerometer + gyro + magnetometer (GAME rate)...", g.CLR_CYN)
        threading.Thread(target=self._coords_worker, daemon=True).start()
        self._refresh_buttons()

    _SENSORS = (("accel", "ACC"), ("gyro", "GYR"), ("mag", "MAG"))

    def _coords_worker(self):
        self._load_coord_fonts()
        W, H = g.W, g.H
        ids = {"accel": g.SENSOR_ACCEL, "gyro": g.SENSOR_GYRO,
               "mag": g.SENSOR_MAG, "light": g.SENSOR_LIGHT}
        s = self.session
        s.sensor_ts.clear()
        s.sensor_count = 0
        for sid in ids.values():           # initial arm at FASTEST rate
            s.start_sensor(sid, rate=1)
            time.sleep(0.05)
        last_log = 0.0
        last_arm = {name: 0.0 for name in ids}
        last_drawn = None

        def fmt(v):
            return f"{v[0]:+7.2f}{v[1]:+7.2f}{v[2]:+7.2f}" if v else "     ...       "

        try:
            while not self._coords_stop.is_set() and self.session:
                s = self.session
                now = time.time()

                # The SED-E1 answers each SensorStart with a short burst then
                # goes quiet, so re-arm aggressively (as soon as >0.15s stale)
                # to drive the update rate as high as the link allows.
                for name, sid in ids.items():
                    age = now - s.sensor_ts.get(name, 0)
                    if age > 0.15 and now - last_arm[name] > 0.15:
                        s.start_sensor(sid, rate=1)
                        last_arm[name] = now

                light = s.sensors.get("light")
                cur = (s.sensors.get("accel"), s.sensors.get("gyro"),
                       s.sensors.get("mag"), light)
                if cur != last_drawn:          # only redraw when values change
                    last_drawn = cur
                    img = Image.new("L", (W, H), 0)
                    d = ImageDraw.Draw(img)
                    d.text((6, 2), "         X      Y      Z", fill=255,
                           font=self._coord_font_small)
                    for i, (key, label) in enumerate(self._SENSORS):
                        v = s.sensors.get(key)
                        d.text((6, 24 + i * 24), f"{label}{fmt(v)}",
                               fill=255 if v else 150, font=self._coord_font_small)
                    lux = f"{light[0]}" if light else "..."
                    acc = cur[0]
                    tilt = f"  tilt {acc[0]:+5.1f},{acc[1]:+5.1f}" if acc else ""
                    d.text((6, 100), f"LUX {lux}{tilt}", fill=255,
                           font=self._coord_font_small)
                    s.send_cmd(g.build_layout_cmd(img.tobytes(), self.jlog), "COORDS frame")

                if now - last_log > 0.5:
                    g.log("COORDS [{} samples]  ACC{}  GYR{}  MAG{}  LUX {}".format(
                        s.sensor_count, fmt(s.sensors.get("accel")),
                        fmt(s.sensors.get("gyro")), fmt(s.sensors.get("mag")),
                        light[0] if light else "..."), g.CLR_CYN)
                    last_log = now

                time.sleep(1.0 / 60)           # poll fast; redraw only on change
        except Exception as exc:
            g.log(f"Coordinates error: {exc}", g.CLR_RED)
        finally:
            if self.session:
                for sid in ids.values():
                    self.session.stop_sensor(sid)
                self.session.send_cmd(g.build_layout_cmd(g.pattern_black(), self.jlog),
                                      "COORDS off - black")
            self._coords_on = False
            self.root.after(0, self._refresh_buttons)
            g.log("Coordinates: stopped.", g.CLR_YLW)


    # -- help ----------------------------------------------------------------
    def on_help(self):
        for line, color in HELP_LINES:
            g.log(line, color)


# Capabilities of the Sony SED-E1, reverse-engineered from the host APK.
HELP_LINES = [
    ("================ SONY SED-E1 - CAPABILITIES ================", g.CLR_CYN),
    ("DISPLAY", g.CLR_GRN),
    ("  419x138 monochrome green OLED, 8-bit + raw DEFLATE.", g.CLR_RST),
    ("  cmds: LayoutPlaceRemove 0xE7, LayoutInit 0xE0, ClearScreen 0xB1,", g.CLR_RST),
    ("        SetScreenState 0x3D, ShiftObject (scroll), OpenAppMode 0xC3.", g.CLR_RST),
    ("  paths: Bluetooth ~2.5fps  |  WiFi ~30fps.", g.CLR_RST),
    ("MOTION / ENVIRONMENT SENSORS  (start 0x38 / stop 0x39)", g.CLR_GRN),
    ("  Accelerometer  id 1   data 0x3A   x/y/z  m/s^2", g.CLR_RST),
    ("  Rotation Vector id 12  data 0xBB   quaternion", g.CLR_RST),
    ("  Gyroscope      id 13  data 0xBC   x/y/z  rad/s", g.CLR_RST),
    ("  Magnetometer   id 14  data 0xBD   x/y/z  uT", g.CLR_RST),
    ("  Light          id 16  data 0x3B   lux", g.CLR_RST),
    ("  Battery               data 0x3E   level", g.CLR_RST),
    ("  rates: 1=FASTEST 2=GAME 3=NORMAL 4=UI 5=INTERRUPT", g.CLR_RST),
    ("CAMERA  (Mode 0xCE -> SensorStart 19 -> CaptureReq 0xB4)", g.CLR_GRN),
    ("  Still 3MP; resolutions 3M/SXGA/XGA/SVGA/VGA/HVGA/QVGA/QQVGA.", g.CLR_RST),
    ("  Quality Standard/Fine/SuperFine. Also JPEG-stream + movie modes.", g.CLR_RST),
    ("MICROPHONE", g.CLR_GRN),
    ("  Standard Bluetooth Hands-Free (HFP) headset mic, 8 kHz.", g.CLR_RST),
    ("INPUT EVENTS (glasses -> host)", g.CLR_GRN),
    ("  Touch 0x32 (short/long press), Key/controller 0x3C, Swipe 0x3F,", g.CLR_RST),
    ("  LevelNotification 0x06 (alignment / tap).", g.CLR_RST),
    ("CONNECTIVITY", g.CLR_GRN),
    ("  Bluetooth SPP (custom RFCOMM ch4) for control; WiFi Direct for fast", g.CLR_RST),
    ("  display (wifi on 0x92 / connect 0x94 / switch 0x96).", g.CLR_RST),
    ("THIS DASHBOARD", g.CLR_GRN),
    ("  Connect (BT->WiFi), Glider, Show Image, Take Photo, Voice ('photo'),", g.CLR_RST),
    ("  Record/Play mic, Coordinates (accel/gyro/mag/light on AR display).", g.CLR_RST),
    ("  Not yet wired to buttons: battery sensor, touch/swipe input,", g.CLR_RST),
    ("  movie recording, scrolling text - all reachable via the protocol.", g.CLR_RST),
    ("===========================================================", g.CLR_CYN),
]


def main():
    root = tk.Tk()
    Dashboard(root)
    root.mainloop()


if __name__ == "__main__":
    main()
