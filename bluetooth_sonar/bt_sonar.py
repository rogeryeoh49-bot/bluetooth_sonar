"""
Bluetooth Sonar Radar
Scans nearby BLE devices, shows them on a radar-style GUI, and emits
sonar-like beeps based on the strongest signal (stronger signal = faster beeps).

Install dependencies:
    pip install bleak sounddevice numpy
"""

import asyncio
import threading
import time
import math
import tkinter as tk
from tkinter import ttk
from dataclasses import dataclass, field
from typing import Dict, Optional, List

import numpy as np
import sounddevice as sd
from bleak import BleakScanner

# ---------- Tunable parameters ----------
RSSI_MIN = -100              # Weakest signal considered (dBm)
RSSI_MAX = -30               # Strongest signal considered (dBm)
PING_INTERVAL_MIN = 0.15     # Fastest beep interval (strongest signal)
PING_INTERVAL_MAX = 2.0      # Slowest beep interval (weakest signal)
BEEP_FREQ_BASE = 600         # Base beep frequency (Hz)
BEEP_FREQ_RANGE = 800        # Extra pitch added at max signal strength
BEEP_DURATION = 0.08         # Duration of a single beep (seconds)
DEVICE_TIMEOUT = 12          # Seconds before an unseen device is dropped

RADAR_SIZE_BASE = 560        # Base radar canvas size (pixels) at scale = 1.0
RADAR_RINGS = 4              # Number of concentric rings
MIN_SCALE = 0.5
MAX_SCALE = 2.0
SCALE_STEP = 0.1

SAMPLE_RATE = 44100
audio_lock = threading.Lock()  # prevents overlapping sd.play calls between threads

ASSUMED_MEASURED_POWER = -59  # typical RSSI at 1 meter, used when tx_power is unknown
PATH_LOSS_EXPONENT = 2.0      # free-space-ish assumption for distance estimate

APPLE_COMPANY_ID = 0x004C

# Partial known BLE company identifier map (Bluetooth SIG assigned numbers)
MANUFACTURER_MAP = {
    0x004C: "Apple",
    0x0006: "Microsoft",
    0x00E0: "Google",
    0x0075: "Samsung",
    0x000F: "Broadcom",
    0x0059: "Nordic Semi",
    0x038F: "Xiaomi",
    0x0157: "Huami/Amazfit",
    0x004F: "Sony",
    0x00D2: "AbTemp",
    0x0087: "Garmin",
    0x0002: "Intel",
    0x001D: "Qualcomm",
    0x0301: "Fitbit",
}

# Apple's manufacturer-data "type" byte (first byte after company ID) hints at
# what kind of continuity/proximity broadcast it is. This is a heuristic based
# on publicly documented/reverse-engineered Apple BLE advertisement formats -
# not officially published by Apple, so treat it as a best-effort guess.
APPLE_TYPE_MAP = {
    0x02: "📶 iBeacon",
    0x03: "🔗 AirPrint",
    0x05: "📤 AirDrop",
    0x06: "🏠 HomeKit",
    0x07: "🎧 AirPods",
    0x08: "🔑 Nearby (Action)",
    0x09: "📡 AirPlay Target",
    0x0A: "📺 AirPlay Source",
    0x0B: "⌚ Apple Watch",
    0x0C: "🔄 Handoff",
    0x0D: "📶 WiFi Settings",
    0x0E: "🌐 Hotspot",
    0x0F: "🔍 Find My (offline)",
    0x10: "📱 Nearby Info",
    0x12: "🔍 Find My (Nearby)",
}


# ---------- Data model ----------
@dataclass
class DeviceInfo:
    name: str
    rssi: int
    last_seen: float
    angle: float = field(default_factory=lambda: 0.0)
    manufacturer_id: Optional[int] = None
    service_uuids: List[str] = field(default_factory=list)
    tx_power: Optional[int] = None
    apple_type_byte: Optional[int] = None


class BLEState:
    """Shared BLE device state across scan / sound / GUI threads."""

    def __init__(self):
        self.lock = threading.Lock()
        self.devices: Dict[str, DeviceInfo] = {}
        self.running = True
        self.locked = False  # when True, no new devices are added

    def update(self, address: str, name: str, rssi: int,
               manufacturer_id: Optional[int], service_uuids: List[str],
               tx_power: Optional[int], apple_type_byte: Optional[int]):
        with self.lock:
            if address in self.devices:
                dev = self.devices[address]
                dev.name = name
                dev.rssi = rssi
                dev.last_seen = time.time()
                if manufacturer_id is not None:
                    dev.manufacturer_id = manufacturer_id
                if service_uuids:
                    dev.service_uuids = service_uuids
                if tx_power is not None:
                    dev.tx_power = tx_power
                if apple_type_byte is not None:
                    dev.apple_type_byte = apple_type_byte
            else:
                if self.locked:
                    return  # list is locked, ignore newly discovered devices
                angle = hash(address) % 360
                self.devices[address] = DeviceInfo(
                    name=name, rssi=rssi, last_seen=time.time(), angle=angle,
                    manufacturer_id=manufacturer_id, service_uuids=service_uuids,
                    tx_power=tx_power, apple_type_byte=apple_type_byte
                )

    def snapshot(self):
        with self.lock:
            if not self.locked:
                now = time.time()
                expired = [a for a, d in self.devices.items() if now - d.last_seen > DEVICE_TIMEOUT]
                for a in expired:
                    del self.devices[a]
            return dict(self.devices)

    def get(self, address: str) -> Optional[DeviceInfo]:
        with self.lock:
            return self.devices.get(address)

    def strongest(self) -> Optional[DeviceInfo]:
        snap = self.snapshot()
        if not snap:
            return None
        return max(snap.values(), key=lambda d: d.rssi)

    def set_locked(self, value: bool):
        with self.lock:
            self.locked = value


class SoundState:
    """Global sound on/off toggle, shared across threads."""

    def __init__(self):
        self.lock = threading.Lock()
        self.enabled = True

    def toggle(self) -> bool:
        with self.lock:
            self.enabled = not self.enabled
            return self.enabled

    def is_enabled(self) -> bool:
        with self.lock:
            return self.enabled


class ImmersiveFlag:
    """When an immersive tracking window is open, pause the main sonar sound."""

    def __init__(self):
        self.lock = threading.Lock()
        self.active = False

    def set(self, value: bool):
        with self.lock:
            self.active = value

    def is_active(self) -> bool:
        with self.lock:
            return self.active


state = BLEState()
sound_state = SoundState()
immersive_flag = ImmersiveFlag()


# ---------- Helper functions ----------
def rssi_to_ratio(rssi: int) -> float:
    rssi_clamped = max(RSSI_MIN, min(RSSI_MAX, rssi))
    return (rssi_clamped - RSSI_MIN) / (RSSI_MAX - RSSI_MIN)


def rssi_to_interval_and_freq(rssi: int):
    ratio = rssi_to_ratio(rssi)
    interval = PING_INTERVAL_MAX - ratio * (PING_INTERVAL_MAX - PING_INTERVAL_MIN)
    freq = BEEP_FREQ_BASE + ratio * BEEP_FREQ_RANGE
    return interval, freq


def signal_bar(rssi: int) -> str:
    ratio = rssi_to_ratio(rssi)
    blocks = "▂▄▆█"
    level = min(len(blocks) - 1, int(ratio * len(blocks)))
    filled = blocks[:level + 1]
    empty = "·" * (len(blocks) - level - 1)
    return filled + empty


def estimate_distance(rssi: int, tx_power: Optional[int]) -> str:
    measured_power = tx_power if tx_power is not None else ASSUMED_MEASURED_POWER
    try:
        distance = 10 ** ((measured_power - rssi) / (10 * PATH_LOSS_EXPONENT))
    except (OverflowError, ValueError):
        return "N/A"
    if distance < 1:
        return f"~{distance * 100:.0f} cm"
    return f"~{distance:.1f} m"


def format_last_seen(last_seen: float) -> str:
    delta = time.time() - last_seen
    if delta < 1:
        return "now"
    return f"{delta:.0f}s ago"


def guess_manufacturer(manufacturer_id: Optional[int]) -> str:
    if manufacturer_id is None:
        return "Unknown"
    return MANUFACTURER_MAP.get(manufacturer_id, f"Unknown (0x{manufacturer_id:04X})")


def guess_device_type(name: str, manufacturer_id: Optional[int] = None,
                       apple_type_byte: Optional[int] = None) -> str:
    n = (name or "").lower().strip()

    # 1) Try name-based keyword matching first - most reliable when present
    if any(k in n for k in ("buds", "airpods", "headphone", "headset", "earphone")):
        return "🎧 Audio"
    if any(k in n for k in ("watch", "band", "fit")):
        return "⌚ Wearable"
    if "pencil" in n:
        return "✏️ Stylus"
    if "mouse" in n:
        return "🖱️ Mouse"
    if "keyboard" in n:
        return "⌨️ Keyboard"
    if "speaker" in n or "soundbar" in n:
        return "🔊 Speaker"
    if "tv" in n or "appletv" in n:
        return "📺 TV"
    if any(k in n for k in ("iphone", "phone", "pixel", "galaxy")):
        return "📱 Phone"
    if any(k in n for k in ("macbook", "laptop", "pc", "imac", "ipad")):
        return "💻 Computer/Tablet"
    if "car" in n or "carplay" in n:
        return "🚗 Car"

    # 2) Name is missing/generic (e.g. "Unknown Device") - fall back to
    #    Apple's manufacturer-data type byte if this is an Apple broadcast
    is_unnamed = (not n) or n == "unknown device"
    if is_unnamed and manufacturer_id == APPLE_COMPANY_ID:
        if apple_type_byte is not None and apple_type_byte in APPLE_TYPE_MAP:
            return APPLE_TYPE_MAP[apple_type_byte]
        return "🍎 Apple Device"

    if is_unnamed:
        return "❓ Unknown"

    return "❓ Other"


def format_services(uuids: List[str]) -> str:
    if not uuids:
        return "-"
    return f"{len(uuids)} svc(s)"


# ---------- Audio ----------
def play_beep(freq: float):
    t = np.linspace(0, BEEP_DURATION, int(SAMPLE_RATE * BEEP_DURATION), False)
    envelope = np.ones_like(t)
    fade_len = max(1, int(SAMPLE_RATE * 0.01))
    envelope[:fade_len] = np.linspace(0, 1, fade_len)
    envelope[-fade_len:] = np.linspace(1, 0, fade_len)
    wave = 0.4 * np.sin(2 * np.pi * freq * t) * envelope
    with audio_lock:
        sd.play(wave.astype(np.float32), SAMPLE_RATE)
        sd.wait()


# ---------- Bluetooth scan thread ----------
def ble_scan_thread():
    async def scan_loop():
        def detection_callback(device, advertisement_data):
            name = device.name or advertisement_data.local_name or "Unknown Device"
            rssi = advertisement_data.rssi
            if rssi is None:
                rssi = getattr(device, "rssi", None)
            if rssi is None:
                return

            manufacturer_id = None
            apple_type_byte = None
            if advertisement_data.manufacturer_data:
                manufacturer_id = next(iter(advertisement_data.manufacturer_data.keys()), None)
                if manufacturer_id == APPLE_COMPANY_ID:
                    raw_bytes = advertisement_data.manufacturer_data.get(APPLE_COMPANY_ID)
                    if raw_bytes:
                        apple_type_byte = raw_bytes[0]

            service_uuids = list(advertisement_data.service_uuids) if advertisement_data.service_uuids else []
            tx_power = getattr(advertisement_data, "tx_power", None)

            state.update(device.address, name, rssi, manufacturer_id, service_uuids,
                         tx_power, apple_type_byte)

        scanner = BleakScanner(detection_callback=detection_callback)
        await scanner.start()
        try:
            while state.running:
                await asyncio.sleep(1)
        finally:
            await scanner.stop()

    asyncio.run(scan_loop())


# ---------- Main sonar sound thread (tracks strongest device overall) ----------
def sonar_sound_thread():
    while state.running:
        if immersive_flag.is_active():
            time.sleep(0.3)
            continue

        best = state.strongest()
        if best is None:
            time.sleep(0.3)
            continue

        interval, freq = rssi_to_interval_and_freq(best.rssi)

        if sound_state.is_enabled():
            play_beep(freq)
            time.sleep(max(0.0, interval - BEEP_DURATION))
        else:
            time.sleep(interval)


# ---------- Immersive single-device tracking window ----------
class ImmersiveWindow:
    """Minimal tracking view: no radar, just signal strength (small, top)
    and predicted distance (large, bottom), both in green."""

    def __init__(self, parent_app: "RadarApp", address: str):
        self.parent_app = parent_app
        self.address = address
        self.running = True

        immersive_flag.set(True)

        self.win = tk.Toplevel(parent_app.root)
        self.win.title(f"Tracking Device - {address}")
        self.win.configure(bg="#04070a")
        self.win.geometry("480x420")
        self.win.protocol("WM_DELETE_WINDOW", self.on_back)

        top_bar = tk.Frame(self.win, bg="#04070a")
        top_bar.pack(fill=tk.X, padx=10, pady=8)

        back_btn = tk.Button(top_bar, text="< Back", command=self.on_back,
                              bg="#000000", fg="#7CFC00", font=("Consolas", 11, "bold"),
                              relief=tk.FLAT, padx=10, pady=4)
        back_btn.pack(side=tk.LEFT)

        self.name_label = tk.Label(top_bar, text="Device: ...", fg="#e0e0e0", bg="#04070a",
                                    font=("Consolas", 11))
        self.name_label.pack(side=tk.LEFT, padx=(16, 0))

        center_frame = tk.Frame(self.win, bg="#04070a")
        center_frame.pack(fill=tk.BOTH, expand=True)

        # Smaller signal strength label, on top
        self.rssi_label = tk.Label(center_frame, text="-- dBm", fg="#00ff41", bg="#04070a",
                                    font=("Consolas", 26, "bold"))
        self.rssi_label.pack(pady=(30, 10))

        # Larger predicted distance label, below
        self.distance_label = tk.Label(center_frame, text="-- m", fg="#00ff41", bg="#04070a",
                                        font=("Consolas", 64, "bold"))
        self.distance_label.pack(pady=(10, 20))

        self.status_label = tk.Label(self.win, text="Tracking...", fg="#4a8a4a", bg="#04070a",
                                      font=("Consolas", 10))
        self.status_label.pack(pady=(0, 12))

        self.update_view()
        self.sound_thread = threading.Thread(target=self.sound_loop, daemon=True)
        self.sound_thread.start()

    def update_view(self):
        if not self.running:
            return

        dev = state.get(self.address)
        if dev:
            self.name_label.config(text=f"Device: {dev.name}")
            self.rssi_label.config(text=f"{dev.rssi} dBm  {signal_bar(dev.rssi)}")
            self.distance_label.config(text=estimate_distance(dev.rssi, dev.tx_power))
            self.status_label.config(text="Tracking...")
        else:
            self.name_label.config(text="Device: (out of range)")
            self.rssi_label.config(text="-- dBm")
            self.distance_label.config(text="-- m")
            self.status_label.config(text="Device not detected - waiting for signal...")

        self.win.after(200, self.update_view)

    def sound_loop(self):
        while self.running:
            dev = state.get(self.address)
            if dev and sound_state.is_enabled():
                interval, freq = rssi_to_interval_and_freq(dev.rssi)
                play_beep(freq)
                time.sleep(max(0.0, interval - BEEP_DURATION))
            else:
                time.sleep(0.3)

    def on_back(self):
        self.running = False
        immersive_flag.set(False)
        self.win.destroy()


# ---------- Main GUI ----------
class RadarApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Bluetooth Sonar Radar")
        self.root.configure(bg="#0b1116")

        self.scale = 1.0

        # ----- Top control bar -----
        control_frame = tk.Frame(root, bg="#0b1116")
        control_frame.pack(fill=tk.X, padx=10, pady=(10, 0))

        self.sound_btn = tk.Button(control_frame, text="Sound: ON", command=self.toggle_sound,
                                    bg="#000000", fg="#7CFC00", font=("Consolas", 11, "bold"),
                                    relief=tk.FLAT, padx=10, pady=4)
        self.sound_btn.pack(side=tk.LEFT, padx=(0, 8))

        self.lock_btn = tk.Button(control_frame, text="Lock List: OFF", command=self.toggle_lock,
                                   bg="#000000", fg="#7CFC00", font=("Consolas", 11, "bold"),
                                   relief=tk.FLAT, padx=10, pady=4)
        self.lock_btn.pack(side=tk.LEFT, padx=(0, 8))

        self.zoom_hint = tk.Label(control_frame, text="Zoom: Cmd + / Cmd -", fg="#666666",
                                   bg="#0b1116", font=("Consolas", 9))
        self.zoom_hint.pack(side=tk.RIGHT)

        # ----- Main content -----
        main_frame = tk.Frame(root, bg="#0b1116")
        main_frame.pack(fill=tk.BOTH, expand=True)

        self.canvas = tk.Canvas(main_frame, width=RADAR_SIZE_BASE, height=RADAR_SIZE_BASE,
                                 bg="#04070a", highlightthickness=0)
        self.canvas.pack(side=tk.LEFT, padx=10, pady=10)

        list_frame = tk.Frame(main_frame, bg="#0b1116")
        list_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=10, pady=10)

        self.title_label = tk.Label(list_frame, text="Nearby Bluetooth Devices", fg="#7CFC00",
                                     bg="#0b1116", font=("Consolas", 14, "bold"))
        self.title_label.pack(anchor="w")

        self.hint_label = tk.Label(list_frame, text="Click a device to lock the list and track it in an immersive view",
                                    fg="#666666", bg="#0b1116", font=("Consolas", 9))
        self.hint_label.pack(anchor="w", pady=(0, 6))

        self.style = ttk.Style()
        self.style.theme_use("default")
        self.style.configure("Treeview", background="#0b1116", fieldbackground="#0b1116",
                              foreground="#e0e0e0", rowheight=24)
        self.style.configure("Treeview.Heading", background="#132029", foreground="#7CFC00")

        tree_container = tk.Frame(list_frame, bg="#0b1116")
        tree_container.pack(fill=tk.BOTH, expand=True)

        columns = ("name", "address", "rssi", "signal", "distance",
                   "last_seen", "manufacturer", "type", "tx_power", "services")
        self.tree = ttk.Treeview(tree_container, columns=columns, show="headings", height=20)

        headings = {
            "name": ("Device Name", 150),
            "address": ("MAC Address", 150),
            "rssi": ("Signal (dBm)", 90),
            "signal": ("Signal Bar", 80),
            "distance": ("Distance", 80),
            "last_seen": ("Last Seen", 90),
            "manufacturer": ("Manufacturer", 120),
            "type": ("Type", 140),
            "tx_power": ("TX Power", 80),
            "services": ("Services", 80),
        }
        for col, (text, width) in headings.items():
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, anchor="center" if col != "name" and col != "address" else "w")

        h_scroll = ttk.Scrollbar(tree_container, orient="horizontal", command=self.tree.xview)
        v_scroll = ttk.Scrollbar(tree_container, orient="vertical", command=self.tree.yview)
        self.tree.configure(xscrollcommand=h_scroll.set, yscrollcommand=v_scroll.set)

        self.tree.grid(row=0, column=0, sticky="nsew")
        v_scroll.grid(row=0, column=1, sticky="ns")
        h_scroll.grid(row=1, column=0, sticky="ew")
        tree_container.grid_rowconfigure(0, weight=1)
        tree_container.grid_columnconfigure(0, weight=1)

        self.tree.bind("<<TreeviewSelect>>", self.on_device_select)

        self.status_label = tk.Label(list_frame, text="Scanning...", fg="#7CFC00", bg="#0b1116",
                                      font=("Consolas", 10))
        self.status_label.pack(anchor="w", pady=(6, 0))

        # ----- Radar geometry -----
        self.radar_size = RADAR_SIZE_BASE
        self.center = self.radar_size // 2
        self.max_radius = self.radar_size // 2 - 30
        self.sweep_angle = 0

        # ----- Keyboard shortcuts for zoom (Cmd on Mac, Ctrl elsewhere) -----
        for seq in ("<Command-equal>", "<Command-plus>", "<Control-equal>", "<Control-plus>"):
            self.root.bind_all(seq, lambda e: self.zoom(SCALE_STEP))
        for seq in ("<Command-minus>", "<Control-minus>"):
            self.root.bind_all(seq, lambda e: self.zoom(-SCALE_STEP))

        self.update_radar()
        self.update_list()

    # ----- Controls -----
    def toggle_sound(self):
        enabled = sound_state.toggle()
        self.sound_btn.config(text=f"Sound: {'ON' if enabled else 'OFF'}",
                               fg="#7CFC00" if enabled else "#ff5555")

    def toggle_lock(self):
        new_state = not state.locked
        state.set_locked(new_state)
        self.lock_btn.config(text=f"Lock List: {'ON' if new_state else 'OFF'}",
                              fg="#ffcc00" if new_state else "#7CFC00")

    def on_device_select(self, event):
        selection = self.tree.selection()
        if not selection:
            return
        item = self.tree.item(selection[0])
        address = item["values"][1]
        self.tree.selection_remove(selection[0])  # avoid re-triggering on same click

        # Clicking a device auto-locks the list
        if not state.locked:
            state.set_locked(True)
            self.lock_btn.config(text="Lock List: ON", fg="#ffcc00")

        ImmersiveWindow(self, str(address))

    def zoom(self, delta):
        self.scale = round(min(MAX_SCALE, max(MIN_SCALE, self.scale + delta)), 2)
        self.radar_size = int(RADAR_SIZE_BASE * self.scale)
        self.canvas.config(width=self.radar_size, height=self.radar_size)
        self.center = self.radar_size // 2
        self.max_radius = self.radar_size // 2 - int(30 * self.scale)

        self.title_label.config(font=("Consolas", max(9, int(14 * self.scale)), "bold"))
        self.hint_label.config(font=("Consolas", max(7, int(9 * self.scale))))
        self.status_label.config(font=("Consolas", max(7, int(10 * self.scale))))
        self.sound_btn.config(font=("Consolas", max(8, int(11 * self.scale)), "bold"))
        self.lock_btn.config(font=("Consolas", max(8, int(11 * self.scale)), "bold"))
        self.style.configure("Treeview", rowheight=max(16, int(24 * self.scale)),
                              font=("Consolas", max(7, int(10 * self.scale))))
        self.style.configure("Treeview.Heading", font=("Consolas", max(8, int(10 * self.scale)), "bold"))

    # ----- Radar drawing -----
    def rssi_to_radius(self, rssi: int) -> float:
        ratio = rssi_to_ratio(rssi)
        return self.max_radius * (1 - ratio) + 15 * self.scale

    def draw_radar_background(self):
        cx = cy = self.center
        for i in range(1, RADAR_RINGS + 1):
            r = self.max_radius * i / RADAR_RINGS
            self.canvas.create_oval(cx - r, cy - r, cx + r, cy + r, outline="#0f3d1f", width=1)
        self.canvas.create_line(cx - self.max_radius, cy, cx + self.max_radius, cy, fill="#0f3d1f")
        self.canvas.create_line(cx, cy - self.max_radius, cx, cy + self.max_radius, fill="#0f3d1f")

    def update_radar(self):
        self.canvas.delete("all")
        self.draw_radar_background()
        cx = cy = self.center

        self.sweep_angle = (self.sweep_angle + 4) % 360
        rad = math.radians(self.sweep_angle)
        x2 = cx + self.max_radius * math.cos(rad)
        y2 = cy + self.max_radius * math.sin(rad)
        self.canvas.create_line(cx, cy, x2, y2, fill="#00ff41", width=2)

        for i in range(1, 20):
            trail_angle = math.radians(self.sweep_angle - i * 2)
            tx = cx + self.max_radius * math.cos(trail_angle)
            ty = cy + self.max_radius * math.sin(trail_angle)
            fade = max(0, 40 - i * 2)
            color = f"#{fade:02x}{min(255, fade * 4):02x}{fade:02x}"
            self.canvas.create_line(cx, cy, tx, ty, fill=color, width=1)

        devices = state.snapshot()
        font_size = max(6, int(8 * self.scale))
        for addr, dev in devices.items():
            radius = self.rssi_to_radius(dev.rssi)
            angle_rad = math.radians(dev.angle)
            x = cx + radius * math.cos(angle_rad)
            y = cy + radius * math.sin(angle_rad)

            ratio = rssi_to_ratio(dev.rssi)
            size = (4 + ratio * 8) * self.scale
            color = "#00ff88" if ratio > 0.5 else ("#ffcc00" if ratio > 0.2 else "#ff5555")

            self.canvas.create_oval(x - size, y - size, x + size, y + size, fill=color, outline="")
            label = dev.name if len(dev.name) <= 12 else dev.name[:12] + "..."
            self.canvas.create_text(x, y - size - 10, text=label, fill="#cccccc",
                                     font=("Consolas", font_size))

        self.root.after(40, self.update_radar)

    def update_list(self):
        devices = state.snapshot()

        if not state.locked:
            for row in self.tree.get_children():
                self.tree.delete(row)

            sorted_devices = sorted(devices.items(), key=lambda kv: kv[1].rssi, reverse=True)
            for addr, dev in sorted_devices:
                self.tree.insert("", tk.END, values=self._row_values(addr, dev))
        else:
            # locked: refresh values in place without touching the row set
            existing = {self.tree.item(i)["values"][1]: i for i in self.tree.get_children()}
            for addr, item_id in existing.items():
                dev = state.get(str(addr))
                if dev:
                    self.tree.item(item_id, values=self._row_values(str(addr), dev))

        lock_text = " (locked)" if state.locked else ""
        self.status_label.config(text=f"{len(devices)} device(s) found{lock_text} | scanning continuously")
        self.root.after(1000, self.update_list)

    @staticmethod
    def _row_values(addr: str, dev: DeviceInfo):
        return (
            dev.name,
            addr,
            dev.rssi,
            signal_bar(dev.rssi),
            estimate_distance(dev.rssi, dev.tx_power),
            format_last_seen(dev.last_seen),
            guess_manufacturer(dev.manufacturer_id),
            guess_device_type(dev.name, dev.manufacturer_id, dev.apple_type_byte),
            dev.tx_power if dev.tx_power is not None else "N/A",
            format_services(dev.service_uuids),
        )


def main():
    ble_thread = threading.Thread(target=ble_scan_thread, daemon=True)
    ble_thread.start()

    sound_thread = threading.Thread(target=sonar_sound_thread, daemon=True)
    sound_thread.start()

    root = tk.Tk()
    RadarApp(root)

    def on_close():
        state.running = False
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
