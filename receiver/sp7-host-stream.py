#!/usr/bin/env python3
"""
SP7 Host Streaming Server
=========================
Runs on the host PC. Captures the desktop, encodes it to H.264, and
streams it as RTP/UDP to the Surface Pro 7 receiver. Also receives
pen/touch input from the Surface and injects it locally via uinput.

Extended display vs mirror
--------------------------
Two display modes, switchable at runtime:
* extend (default): the host creates a headless virtual output at the
  Surface's 3:2 resolution — the Surface becomes a real second monitor
  with its own desktop.
* mirror: the Surface duplicates an existing screen.

Start in a mode with --mode extend|mirror (or --mirror). Switch a running
host between the two at any time with `sp7-host-stream.py --toggle`, which
signals the running instance (no restart, no dropped connection).

Capture sources
---------------
* X11 sessions: `ximagesrc` works directly (mirror only).
* Wayland sessions (Hyprland): the virtual output — or, in mirror mode, an
  existing output — is captured via wf-recorder (wlr-screencopy).

Usage: sp7-host-stream.py [--target <SP7_IP>] [--mode extend|mirror]
                          [--toggle] [--display WxH] [--fps N]
                          [--bitrate KBPS] [--width W] [--no-input] [-v]
"""

import os
import sys
import json
import time
import socket
import signal
import logging
import argparse
import subprocess
import threading
from typing import Optional

import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib

try:
    import msgpack
except ImportError:
    print("ERROR: msgpack not installed (pip3 install msgpack)", file=sys.stderr)
    sys.exit(1)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%H:%M:%S',
)
logger = logging.getLogger('sp7-host')

DEFAULT_VIDEO_PORT = 5004
DEFAULT_INPUT_PORT = 5005
DISCOVERY_PORT = 5006
DEFAULT_FPS = 30
# 8 Mbps: 12 Mbps saturated typical 2.4/5GHz Wi-Fi, causing bursty packet
# loss (H.264 macroblock glitches). 8 Mbps leaves headroom for the link.
DEFAULT_BITRATE = 8000  # kbps

# Virtual extended-display defaults —1080p at the Surface's 3:2 aspect
# (1620x1080). The receiver hardware-upscales it to the 2736x1824 panel;
# both are 3:2, so it fills the screen with no distortion or black bars.
DEFAULT_DISPLAY_W = 1620
DEFAULT_DISPLAY_H = 1080

# uinput touch device name, and the slug Hyprland derives from it.
TOUCH_DEVICE_NAME = 'SP7 Virtual Touchscreen'
TOUCH_DEVICE_HYPR = 'sp7-virtual-touchscreen'

# pidfile — lets `sp7-host --toggle` find a running instance to signal.
PIDFILE = '/tmp/sp7-host.pid'


def discover_surface(timeout: float = 6.0) -> Optional[str]:
    """Broadcast a UDP discovery probe and return the Surface's IP address
    (the receiver's DiscoveryResponder answers it), or None if not found."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.settimeout(1.0)
    logger.info("Discovering the Surface on the LAN...")
    deadline = time.time() + timeout
    try:
        while time.time() < deadline:
            try:
                s.sendto(b'SP7?', ('255.255.255.255', DISCOVERY_PORT))
            except OSError as e:
                logger.debug(f"broadcast send failed: {e}")
            try:
                data, addr = s.recvfrom(256)
                if b'SP7-MONITOR' in data:
                    return addr[0]
            except socket.timeout:
                continue
    finally:
        s.close()
    return None


# ---------------------------------------------------------------------------
# Capture / encoder selection
# ---------------------------------------------------------------------------
def _have(element: str) -> bool:
    return Gst.ElementFactory.find(element) is not None


def detect_encoder() -> str:
    """Pick the best available H.264 encoder element."""
    for enc in ('vah264enc', 'vaapih264enc', 'nvh264enc', 'x264enc'):
        if _have(enc):
            return enc
    return 'x264enc'


def detect_capture_source() -> str:
    """Best-effort default capture source. --source overrides this."""
    session = os.environ.get('XDG_SESSION_TYPE', '')
    if session == 'wayland':
        logger.info("Wayland session — capturing via wf-recorder (wlr-screencopy)")
        return 'wfrecorder'
    if _have('ximagesrc'):
        disp = os.environ.get('DISPLAY', ':0')
        logger.info(f"X11 capture via ximagesrc (display {disp})")
        return f'ximagesrc display-name={disp} use-damage=false show-pointer=true'
    logger.warning("No capture source detected — using a test pattern")
    return 'videotestsrc is-live=true pattern=ball'


def build_capture_pipeline(source: str, fps: int, bitrate: int,
                           target_ip: str, target_port: int,
                           width: int = 0) -> str:
    """Assemble the capture -> H.264 -> RTP/UDP pipeline.

    Encoders are tuned for low latency: no B-frames (they require
    reordering, which adds a frame of delay), one reference frame, a
    keyframe every second so the picture self-heals quickly after any
    packet loss, and the fastest speed preset."""
    enc = detect_encoder()
    if enc == 'x264enc':
        encstr = (f'x264enc bitrate={bitrate} speed-preset=ultrafast '
                  f'tune=zerolatency key-int-max={fps} bframes=0 '
                  f'sliced-threads=true')
    elif enc == 'vaapih264enc':
        encstr = (f'vaapih264enc rate-control=cbr bitrate={bitrate} '
                  f'keyframe-period={fps}')
    elif enc == 'vah264enc':
        encstr = (f'vah264enc rate-control=cbr bitrate={bitrate} '
                  f'target-usage=7 key-int-max={fps} ref-frames=1 '
                  f'b-frames=0')
    elif enc == 'nvh264enc':
        encstr = (f'nvh264enc bitrate={bitrate} gop-size={fps} '
                  f'bframes=0 rc-mode=cbr zerolatency=true')
    else:
        encstr = enc

    # Optional downscale before encoding. Default (width=0) streams the
    # capture at native resolution; the receiver hardware-scales it to the
    # Surface panel. videoscale with only the width pinned keeps the aspect
    # ratio. width must be even (H.264 requires even dimensions).
    scale = ''
    if width and width > 0:
        scale = f"videoscale ! video/x-raw,width={width} ! "

    # Leaky queue on raw frames: if the encoder ever falls behind, drop the
    # oldest captured frame rather than letting latency grow. config-interval
    # -1 sends SPS/PPS with every keyframe so the decoder can resync fast.
    pipeline = (
        f"{source} ! queue leaky=downstream max-size-buffers=3 "
        f"max-size-time=0 max-size-bytes=0 ! "
        f"{scale}videoconvert ! {encstr} ! "
        f"h264parse config-interval=-1 ! "
        f"rtph264pay pt=96 mtu=1400 config-interval=-1 ! "
        f"udpsink host={target_ip} port={target_port} sync=false"
    )
    logger.info(f"Encoder: {enc}")
    logger.info(f"Pipeline: {pipeline}")
    return pipeline


# ---------------------------------------------------------------------------
# Wayland capture via wf-recorder (wlr-screencopy, portal-independent)
# ---------------------------------------------------------------------------
class WfRecorderCapture:
    """Captures the Hyprland/wlroots desktop with wf-recorder into a fifo,
    exposed to GStreamer as an `fdsrc`. Raw frames (single hardware encode
    downstream); -D forces continuous frames regardless of screen damage."""

    def __init__(self, output: Optional[str] = None):
        self.output = output   # specific output to capture; None = first
        self.proc = None
        self.fifo = None
        self.fd = None

    def start(self) -> str:
        import tempfile
        mons = json.loads(subprocess.run(
            ['hyprctl', '-j', 'monitors'],
            capture_output=True, text=True, timeout=5).stdout)
        if self.output:
            m = next((x for x in mons if x['name'] == self.output), None)
            if m is None:
                raise RuntimeError(f"capture output '{self.output}' not "
                                   f"found among Hyprland monitors")
        else:
            m = mons[0]
        out, w, h = m['name'], int(m['width']), int(m['height'])
        rate = max(1, round(float(m.get('refreshRate', 60))))
        self.fifo = tempfile.mktemp(prefix='sp7cap-', suffix='.y4m')
        os.mkfifo(self.fifo)
        logger.info(f"wf-recorder: capturing {out} {w}x{h}@{rate}")
        self.proc = subprocess.Popen(
            ['wf-recorder', '-o', out, '-c', 'rawvideo', '-x', 'yuv420p',
             '--muxer=yuv4mpegpipe', '-D', '-y', '-f', self.fifo],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1.5)  # let wf-recorder open the fifo's write end
        if self.proc.poll() is not None:
            raise RuntimeError("wf-recorder exited immediately "
                               "(is it installed? is the output name right?)")
        self.fd = os.open(self.fifo, os.O_RDONLY)
        # capssetter relabels y4mdec's bogus avformat framerate to the real
        # rate (videorate would otherwise stall trying to drop 90000fps).
        return (f'fdsrc fd={self.fd} ! y4mdec ! capssetter replace=true '
                f'caps="video/x-raw,format=I420,width={w},height={h},'
                f'framerate={rate}/1,interlace-mode=progressive,'
                f'pixel-aspect-ratio=1/1" ! videoconvert')

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except Exception:
                self.proc.kill()
        if self.fd is not None:
            try:
                os.close(self.fd)
            except Exception:
                pass
        if self.fifo and os.path.exists(self.fifo):
            try:
                os.unlink(self.fifo)
            except Exception:
                pass
        logger.info("wf-recorder capture stopped")


# ---------------------------------------------------------------------------
# Video streamer
# ---------------------------------------------------------------------------
class VideoStreamer:
    def __init__(self, target_ip, video_port, source, fps, bitrate, width=0):
        self.target_ip = target_ip
        self.video_port = video_port
        self.source = source
        self.fps = fps
        self.bitrate = bitrate
        self.width = width
        self.pipeline: Optional[Gst.Pipeline] = None
        self.loop: Optional[GLib.MainLoop] = None

    def start(self):
        Gst.init(None)
        desc = build_capture_pipeline(self.source, self.fps, self.bitrate,
                                      self.target_ip, self.video_port,
                                      self.width)
        self.pipeline = Gst.parse_launch(desc)
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_bus_message)
        ret = self.pipeline.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("Failed to start streaming pipeline")
        logger.info(f"Streaming to {self.target_ip}:{self.video_port} "
                    f"@ {self.fps}fps {self.bitrate}kbps")
        self.loop = GLib.MainLoop()
        threading.Thread(target=self.loop.run, daemon=True).start()

    def _on_bus_message(self, bus, message):
        t = message.type
        if t == Gst.MessageType.ERROR:
            err, dbg = message.parse_error()
            logger.error(f"GStreamer error: {err.message} ({dbg})")
        elif t == Gst.MessageType.WARNING:
            w, _ = message.parse_warning()
            logger.warning(f"GStreamer warning: {w.message}")
        elif t == Gst.MessageType.STATE_CHANGED and message.src == self.pipeline:
            _, new, _ = message.parse_state_changed()
            if new == Gst.State.PLAYING:
                logger.info("Streaming pipeline is PLAYING")

    def stop(self):
        if self.loop:
            self.loop.quit()
        if self.pipeline:
            self.pipeline.set_state(Gst.State.NULL)
        logger.info("Video streamer stopped")


# ---------------------------------------------------------------------------
# Input server — receives pen/touch from the SP7, injects via uinput
# ---------------------------------------------------------------------------
class InputServer:
    """Receives pen/touch packets from the Surface and replays them on the
    host through a virtual *touchscreen* (uinput).

    It is deliberately a touchscreen, not a pen/tablet: a virtual tablet was
    not picked up by the compositor at all (empty Tablets list). A
    touchscreen — INPUT_PROP_DIRECT plus the multitouch type-B protocol — is
    the same device class as a real laptop touchscreen, which the compositor
    handles out of the box."""

    ABS_MAX = 32767  # logical coord range; libinput maps it onto the output

    def __init__(self, port: int):
        self.port = port
        self._running = False
        self._socket: Optional[socket.socket] = None
        self._uinput = None
        self._slots: dict = {}   # slot -> {'touching': bool}
        self._tid = 0            # monotonic MT tracking-id counter

    def _setup_uinput(self) -> bool:
        try:
            from evdev import UInput, AbsInfo, ecodes as e
        except ImportError:
            logger.error("python-evdev not installed — input injection off")
            return False
        m = self.ABS_MAX
        cap = {
            e.EV_KEY: [e.BTN_TOUCH],
            e.EV_ABS: [
                (e.ABS_X, AbsInfo(0, 0, m, 0, 0, 0)),
                (e.ABS_Y, AbsInfo(0, 0, m, 0, 0, 0)),
                (e.ABS_MT_SLOT, AbsInfo(0, 0, 9, 0, 0, 0)),
                (e.ABS_MT_POSITION_X, AbsInfo(0, 0, m, 0, 0, 0)),
                (e.ABS_MT_POSITION_Y, AbsInfo(0, 0, m, 0, 0, 0)),
                # min -1: the kernel's "contact lifted" sentinel must not be
                # clamped away by uinput's range enforcement.
                (e.ABS_MT_TRACKING_ID, AbsInfo(0, -1, 65535, 0, 0, 0)),
            ],
        }
        try:
            self._uinput = UInput(cap, name=TOUCH_DEVICE_NAME,
                                  input_props=[e.INPUT_PROP_DIRECT],
                                  version=0x3)
        except PermissionError:
            logger.error("No permission for /dev/uinput — run as root or add "
                          "the user to the 'input' group")
            return False
        except Exception as ex:
            logger.error(f"uinput setup failed: {ex}")
            return False
        logger.info("uinput virtual touchscreen created")
        return True

    def _inject(self, pkt: dict):
        """Replay one packet via the type-B multitouch protocol. Each source
        digitizer owns its own slot, so an idle device's tip=False never
        cancels another device's active contact."""
        from evdev import ecodes as e
        d = self._uinput
        slot = min(9, max(0, int(pkt.get('slot', 0))))
        tip = bool(pkt.get('tip'))
        st = self._slots.setdefault(slot, {'touching': False})
        if not tip and not st['touching']:
            return  # idle — nothing to report for this slot
        x = max(0, min(self.ABS_MAX, int(pkt.get('x', 0.0) * self.ABS_MAX)))
        y = max(0, min(self.ABS_MAX, int(pkt.get('y', 0.0) * self.ABS_MAX)))
        d.write(e.EV_ABS, e.ABS_MT_SLOT, slot)
        if tip and not st['touching']:            # contact begins
            st['touching'] = True
            self._tid = (self._tid + 1) & 0xffff
            d.write(e.EV_ABS, e.ABS_MT_TRACKING_ID, self._tid)
            d.write(e.EV_ABS, e.ABS_MT_POSITION_X, x)
            d.write(e.EV_ABS, e.ABS_MT_POSITION_Y, y)
        elif tip:                                 # contact moves
            d.write(e.EV_ABS, e.ABS_MT_POSITION_X, x)
            d.write(e.EV_ABS, e.ABS_MT_POSITION_Y, y)
        else:                                     # contact ends
            st['touching'] = False
            d.write(e.EV_ABS, e.ABS_MT_TRACKING_ID, -1)
        if tip:
            d.write(e.EV_ABS, e.ABS_X, x)
            d.write(e.EV_ABS, e.ABS_Y, y)
        # BTN_TOUCH reflects whether *any* contact is currently down.
        any_touch = any(s['touching'] for s in self._slots.values())
        d.write(e.EV_KEY, e.BTN_TOUCH, 1 if any_touch else 0)
        d.syn()

    def _loop(self):
        try:
            self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._socket.bind(('0.0.0.0', self.port))
            self._socket.settimeout(1.0)
        except Exception as ex:
            logger.error(f"Input socket bind failed: {ex}")
            return
        logger.info(f"Input server listening on UDP :{self.port}")
        count, last = 0, time.time()
        while self._running:
            try:
                data, addr = self._socket.recvfrom(2048)
                pkt = msgpack.unpackb(data, raw=False)
                if self._uinput:
                    self._inject(pkt)
                count += 1
                if time.time() - last >= 10:
                    logger.info(f"Input: {count} events/10s from {addr[0]}")
                    count, last = 0, time.time()
            except socket.timeout:
                continue
            except Exception as ex:
                if self._running:
                    logger.debug(f"Input recv error: {ex}")
        logger.info("Input server stopped")

    def start(self):
        self._running = True
        self._setup_uinput()
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self):
        self._running = False
        if self._socket:
            try:
                self._socket.close()
            except Exception:
                pass
        if self._uinput:
            try:
                self._uinput.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# mDNS advertisement (best-effort)
# ---------------------------------------------------------------------------
class ServiceAdvertiser:
    def __init__(self, port: int):
        self.port = port
        self._proc = None

    def start(self):
        try:
            self._proc = subprocess.Popen(
                ['avahi-publish-service', 'sp7-monitor-host',
                 '_sp7monitor._tcp', str(self.port)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            logger.info("mDNS service advertised (_sp7monitor._tcp)")
        except FileNotFoundError:
            logger.debug("avahi-publish-service not available — skipping mDNS")

    def stop(self):
        if self._proc:
            self._proc.terminate()


# ---------------------------------------------------------------------------
# Discovery beacon
# ---------------------------------------------------------------------------
class DiscoveryBeacon:
    """Periodically broadcasts the discovery probe. The one-shot sweep in
    discover_surface() only runs at host startup; if the Surface receiver
    restarts after that, it would never learn this host's address (and so
    never forward input back). This keeps broadcasting so the receiver's
    DiscoveryResponder always re-learns the host within a few seconds."""

    def __init__(self, port: int, interval: float = 3.0):
        self.port = port
        self.interval = interval
        self._running = False
        self._sock: Optional[socket.socket] = None

    def start(self):
        self._running = True
        threading.Thread(target=self._loop, daemon=True).start()
        logger.info(f"Discovery beacon broadcasting every {self.interval}s")

    def _loop(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        while self._running:
            try:
                self._sock.sendto(b'SP7?', ('255.255.255.255', self.port))
            except OSError as e:
                logger.debug(f"beacon broadcast failed: {e}")
            time.sleep(self.interval)

    def stop(self):
        self._running = False
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Virtual extended display (Hyprland headless output)
# ---------------------------------------------------------------------------
class VirtualDisplay:
    """Creates a Hyprland headless output so the Surface acts as a real
    *extended* monitor — its own 3:2 desktop you can drag windows onto —
    rather than mirroring an existing screen."""

    def __init__(self, width: int, height: int, refresh: int = 60):
        self.width = width
        self.height = height
        self.refresh = refresh
        self.name: Optional[str] = None
        self._created = False

    @staticmethod
    def _hyprctl(*args) -> subprocess.CompletedProcess:
        return subprocess.run(['hyprctl', *args], capture_output=True,
                               text=True, timeout=8)

    @classmethod
    def _headless_outputs(cls) -> list:
        try:
            mons = json.loads(cls._hyprctl('-j', 'monitors').stdout)
            return sorted(m['name'] for m in mons
                          if m['name'].startswith('HEADLESS-'))
        except Exception:
            return []

    def create(self) -> str:
        """Create (or reuse) the headless output and size it. Returns its
        name. Raises if Hyprland is not driving the session."""
        if self._hyprctl('version').returncode != 0:
            raise RuntimeError("hyprctl unavailable (not a Hyprland session)")
        existing = self._headless_outputs()
        if existing:
            # Reuse a headless output left over from an unclean exit rather
            # than stacking phantom monitors.
            self.name = existing[0]
            self._created = False
            logger.info(f"Reusing headless output {self.name}")
        else:
            self._hyprctl('output', 'create', 'headless')
            time.sleep(0.6)
            new = self._headless_outputs()
            if not new:
                raise RuntimeError("Hyprland did not create a headless output")
            self.name = new[0]
            self._created = True
        self._hyprctl('keyword', 'monitor',
                      f'{self.name},{self.width}x{self.height}@'
                      f'{self.refresh},auto,1')
        time.sleep(0.4)
        logger.info(f"Virtual display: {self.name} "
                    f"{self.width}x{self.height} (extended desktop)")
        return self.name

    def destroy(self) -> None:
        if self.name and self._created:
            self._hyprctl('output', 'remove', self.name)
            logger.info(f"Virtual display {self.name} removed")


# ---------------------------------------------------------------------------
# Hyprland helpers shared by both display modes
# ---------------------------------------------------------------------------
def primary_output() -> Optional[str]:
    """Name of the first real (non-headless) monitor — the screen that
    'mirror' mode duplicates."""
    try:
        mons = json.loads(subprocess.run(
            ['hyprctl', '-j', 'monitors'],
            capture_output=True, text=True, timeout=8).stdout)
        real = [m for m in mons if not m['name'].startswith('HEADLESS-')]
        return (real or mons)[0]['name']
    except Exception:
        return None


def set_touch_output(device_name: str, output_name: Optional[str]) -> None:
    """Route a touch device's input onto a specific monitor."""
    if not output_name:
        return
    try:
        r = subprocess.run(
            ['hyprctl', 'keyword',
             f'device[{device_name}]:output', output_name],
            capture_output=True, text=True, timeout=8)
        if r.returncode == 0 and 'ok' in r.stdout.lower():
            logger.info(f"Touch input bound to {output_name}")
        else:
            logger.warning(f"Could not bind touch to {output_name}: "
                            f"{r.stdout.strip() or r.stderr.strip()}")
    except Exception as e:
        logger.warning(f"Touch bind failed: {e}")


# ---------------------------------------------------------------------------
# Host server
# ---------------------------------------------------------------------------
class HostServer:
    """Owns the capture/stream lifecycle and can switch, at runtime, between
    'extend' (a headless virtual monitor) and 'mirror' (duplicating an
    existing screen) — see switch_mode()."""

    def __init__(self, target_ip, video_port, input_port, fps, bitrate,
                 enable_input, width=0, mode='extend',
                 display_res=(DEFAULT_DISPLAY_W, DEFAULT_DISPLAY_H),
                 custom_source=None):
        self.target_ip = target_ip
        self.video_port = video_port
        self.input_port = input_port
        self.fps = fps
        self.bitrate = bitrate
        self.enable_input = enable_input
        self.width = width
        self.mode = mode                    # 'extend' or 'mirror'
        self.display_res = display_res
        self.custom_source = custom_source
        self.streamer = None
        self.input_server = None
        self.advertiser = None
        self.beacon = None
        self.capture = None
        self.virtual_display = None
        self._switch_lock = threading.Lock()
        self._stop = threading.Event()

    # -- capture construction ------------------------------------------------
    def _build_capture(self) -> str:
        """Build the capture for the current mode. Sets self.capture and
        self.virtual_display; returns the GStreamer source string."""
        if self.custom_source:
            return self.custom_source

        capture_output = None
        if self.mode == 'extend':
            try:
                dw, dh = self.display_res
                self.virtual_display = VirtualDisplay(dw, dh)
                capture_output = self.virtual_display.create()
            except Exception as e:
                logger.error(f"Virtual display unavailable ({e}) — "
                             f"using mirror mode")
                self.virtual_display = None
                self.mode = 'mirror'

        if self.mode == 'mirror':
            capture_output = primary_output()

        src = detect_capture_source()
        if src != 'wfrecorder':
            # No Hyprland / X11 session — only a direct mirror is possible.
            self.mode = 'mirror'
            return src
        try:
            self.capture = WfRecorderCapture(output=capture_output)
            source = self.capture.start()
            logger.info(f"Capture source ({self.mode}): {source}")
            return source
        except Exception as e:
            logger.error(f"wf-recorder capture failed: {e}")
            if self.virtual_display:
                self.virtual_display.destroy()
                self.virtual_display = None
            return 'videotestsrc is-live=true pattern=ball'

    def _teardown_capture(self) -> None:
        if self.capture:
            self.capture.stop()
            self.capture = None
        if self.virtual_display:
            self.virtual_display.destroy()
            self.virtual_display = None

    def _bind_touch(self) -> None:
        """Route the virtual touchscreen onto whatever the Surface shows:
        the headless output in extend mode, the mirrored screen otherwise."""
        if not (self.enable_input and self.input_server
                and self.input_server._uinput):
            return
        if self.virtual_display:
            set_touch_output(TOUCH_DEVICE_HYPR, self.virtual_display.name)
        else:
            set_touch_output(TOUCH_DEVICE_HYPR, primary_output())

    # -- lifecycle -----------------------------------------------------------
    def start(self) -> None:
        logger.info("=" * 52)
        logger.info("  SP7 Monitor Host Server")
        logger.info(f"  target {self.target_ip}:{self.video_port}  "
                    f"input :{self.input_port}")
        logger.info("=" * 52)
        self.advertiser = ServiceAdvertiser(self.video_port)
        self.advertiser.start()
        # Keep announcing ourselves so the Surface can (re)discover us.
        self.beacon = DiscoveryBeacon(DISCOVERY_PORT)
        self.beacon.start()
        if self.enable_input:
            self.input_server = InputServer(self.input_port)
            self.input_server.start()

        source = self._build_capture()

        if self.enable_input and self.input_server._uinput:
            # Wait for Hyprland to register the hotplugged uinput device.
            time.sleep(1.2)
            self._bind_touch()

        self.streamer = VideoStreamer(self.target_ip, self.video_port,
                                      source, self.fps, self.bitrate,
                                      self.width)
        self.streamer.start()
        logger.info(f"Host server running in '{self.mode}' mode — "
                    f"'sp7-host --toggle' switches extend/mirror, "
                    f"Ctrl+C stops")

    def switch_mode(self) -> None:
        """Toggle between extend and mirror without restarting the server."""
        if not self._switch_lock.acquire(blocking=False):
            logger.warning("A mode switch is already in progress")
            return
        try:
            if self.custom_source:
                logger.warning("Cannot switch mode with an explicit --source")
                return
            new = 'mirror' if self.mode == 'extend' else 'extend'
            logger.info(f"Switching display mode: {self.mode} -> {new}")
            if self.streamer:
                self.streamer.stop()
                self.streamer = None
            self._teardown_capture()
            self.mode = new
            source = self._build_capture()
            self._bind_touch()
            self.streamer = VideoStreamer(self.target_ip, self.video_port,
                                          source, self.fps, self.bitrate,
                                          self.width)
            self.streamer.start()
            logger.info(f"Display mode is now '{self.mode}'")
        except Exception as e:
            logger.error(f"Mode switch failed: {e}")
        finally:
            self._switch_lock.release()

    def stop(self) -> None:
        logger.info("Shutting down host server...")
        self._stop.set()
        if self.streamer:
            self.streamer.stop()
        if self.input_server:
            self.input_server.stop()
        if self.beacon:
            self.beacon.stop()
        if self.advertiser:
            self.advertiser.stop()
        self._teardown_capture()

    def run(self) -> None:
        try:
            self.start()
            while not self._stop.is_set():
                self._stop.wait(1.0)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()


def _toggle_running_instance() -> int:
    """Signal an already-running host to switch display mode."""
    try:
        with open(PIDFILE) as f:
            pid = int(f.read().strip())
        os.kill(pid, signal.SIGUSR1)
        print(f"Sent display-mode switch to sp7-host (pid {pid}).")
        return 0
    except (FileNotFoundError, ValueError, ProcessLookupError, PermissionError):
        print("No running sp7-host instance found.", file=sys.stderr)
        return 1


def main():
    parser = argparse.ArgumentParser(description='SP7 Monitor Host Streamer')
    parser.add_argument('--target', '-t',
                        help='Surface Pro 7 IP address '
                             '(auto-discovered on the LAN if omitted)')
    parser.add_argument('--video-port', type=int, default=DEFAULT_VIDEO_PORT)
    parser.add_argument('--input-port', type=int, default=DEFAULT_INPUT_PORT)
    parser.add_argument('--source', '-s',
                        help='GStreamer capture source (overrides autodetect), '
                             'e.g. "videotestsrc is-live=true" or a '
                             '"pipewiresrc fd=N path=M" portal source')
    parser.add_argument('--fps', type=int, default=DEFAULT_FPS)
    parser.add_argument('--bitrate', type=int, default=DEFAULT_BITRATE,
                        help='kbps (default: %(default)s)')
    parser.add_argument('--width', type=int, default=0,
                        help='downscale capture to this width before '
                             'encoding (even number; 0 = native, default). '
                             'Lower it if Wi-Fi cannot keep up.')
    parser.add_argument('--mode', choices=['extend', 'mirror'],
                        default='extend',
                        help="'extend' (default): the Surface is a separate "
                             "monitor with its own desktop. 'mirror': it "
                             "duplicates an existing screen.")
    parser.add_argument('--mirror', action='store_true',
                        help='shorthand for --mode mirror')
    parser.add_argument('--toggle', action='store_true',
                        help='tell a running sp7-host to switch between '
                             'extend and mirror, then exit')
    parser.add_argument('--display',
                        default=f'{DEFAULT_DISPLAY_W}x{DEFAULT_DISPLAY_H}',
                        help='extended-display resolution WxH '
                             '(default: %(default)s)')
    parser.add_argument('--no-input', action='store_true',
                        help='Disable pen/touch input reception')
    parser.add_argument('--verbose', '-v', action='store_true')
    args = parser.parse_args()

    if args.toggle:
        sys.exit(_toggle_running_instance())

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    target = args.target
    if not target:
        target = discover_surface()
        if not target:
            logger.error("No Surface found on the LAN. Boot it and wait for "
                          "Wi-Fi to connect, or pass --target <ip>.")
            sys.exit(1)
        logger.info(f"Found Surface at {target}")

    Gst.init(None)

    mode = 'mirror' if args.mirror else args.mode
    try:
        dw, dh = (int(v) for v in args.display.lower().split('x'))
    except Exception:
        logger.error(f"Invalid --display '{args.display}' — using default")
        dw, dh = DEFAULT_DISPLAY_W, DEFAULT_DISPLAY_H

    # An explicit --source is a fixed custom pipeline and disables mode
    # switching. '--source portal' resolves to an xdg-desktop-portal capture.
    custom_source = args.source
    if custom_source == 'portal':
        logger.info("Opening Wayland desktop capture via xdg-desktop-portal...")
        try:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            import portal_screencast
            fd, node = portal_screencast.open_screencast()
            custom_source = portal_screencast.gst_source(fd, node)
            logger.info(f"Screencast source: {custom_source}")
        except Exception as e:
            logger.error(f"Portal screencast failed: {e}")
            custom_source = 'videotestsrc is-live=true pattern=ball'

    server = HostServer(target, args.video_port, args.input_port,
                        args.fps, args.bitrate, not args.no_input,
                        args.width, mode, (dw, dh), custom_source)

    # pidfile so `sp7-host --toggle` can find this instance to signal.
    try:
        with open(PIDFILE, 'w') as f:
            f.write(str(os.getpid()))
    except OSError:
        pass

    signal.signal(signal.SIGINT, lambda *_: server.stop())
    signal.signal(signal.SIGTERM, lambda *_: server.stop())
    # SIGUSR1 -> switch display mode (handled off the signal thread).
    signal.signal(signal.SIGUSR1,
                  lambda *_: threading.Thread(target=server.switch_mode,
                                              daemon=True).start())
    try:
        server.run()
    finally:
        try:
            os.remove(PIDFILE)
        except OSError:
            pass


if __name__ == '__main__':
    main()
