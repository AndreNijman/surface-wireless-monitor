#!/usr/bin/env python3
"""
SP7 Host Streaming Server
=========================
Runs on the host PC. Captures the desktop, encodes it to H.264, and
streams it as RTP/UDP to the Surface Pro 7 receiver. Also receives
pen/touch input from the Surface and injects it locally via uinput.

Capture sources
---------------
* X11 sessions: `ximagesrc` works directly.
* Wayland sessions (Hyprland/sway/...): X11 capture sees only a black
  XWayland root. Use --source with a PipeWire screencast portal source
  (see receiver/portal_screencast.py), or --source "videotestsrc is-live=true"
  for a test pattern.

Usage: sp7-host-stream.py --target <SP7_IP> [--source GST_SRC] [--fps N]
                          [--bitrate KBPS] [--no-input] [--verbose]
"""

import os
import sys
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

    def __init__(self):
        self.proc = None
        self.fifo = None
        self.fd = None

    def start(self) -> str:
        import json
        import tempfile
        mons = json.loads(subprocess.run(
            ['hyprctl', '-j', 'monitors'],
            capture_output=True, text=True, timeout=5).stdout)
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
    def __init__(self, port: int):
        self.port = port
        self._running = False
        self._socket: Optional[socket.socket] = None
        self._uinput = None
        self.screen_w, self.screen_h = self._screen_size()

    @staticmethod
    def _screen_size() -> tuple:
        # Hyprland
        try:
            import json
            out = subprocess.run(['hyprctl', '-j', 'monitors'],
                                 capture_output=True, text=True, timeout=5)
            mons = json.loads(out.stdout)
            if mons:
                m = mons[0]
                return int(m['width']), int(m['height'])
        except Exception:
            pass
        # X11 / XWayland
        try:
            out = subprocess.run(['xrandr'], capture_output=True,
                                 text=True, timeout=5)
            for line in out.stdout.splitlines():
                if '*' in line:
                    w, h = line.split()[0].split('x')
                    return int(w), int(h)
        except Exception:
            pass
        return 1920, 1080

    def _setup_uinput(self) -> bool:
        try:
            from evdev import UInput, AbsInfo, ecodes as e
        except ImportError:
            logger.error("python-evdev not installed — input injection off")
            return False
        cap = {
            e.EV_KEY: [e.BTN_TOOL_PEN, e.BTN_TOUCH, e.BTN_STYLUS],
            e.EV_ABS: [
                (e.ABS_X, AbsInfo(0, 0, self.screen_w, 0, 0, 0)),
                (e.ABS_Y, AbsInfo(0, 0, self.screen_h, 0, 0, 0)),
                (e.ABS_PRESSURE, AbsInfo(0, 0, 4095, 0, 0, 0)),
                (e.ABS_TILT_X, AbsInfo(0, -90, 90, 0, 0, 0)),
                (e.ABS_TILT_Y, AbsInfo(0, -90, 90, 0, 0, 0)),
            ],
        }
        try:
            self._uinput = UInput(cap, name='SP7 Virtual Pen', version=0x3)
        except PermissionError:
            logger.error("No permission for /dev/uinput — run as root or add "
                          "the user to the 'input' group")
            return False
        except Exception as ex:
            logger.error(f"uinput setup failed: {ex}")
            return False
        logger.info(f"uinput pen device created ({self.screen_w}x{self.screen_h})")
        return True

    def _inject(self, pkt: dict):
        from evdev import ecodes as e
        d = self._uinput
        x = max(0, min(self.screen_w, int(pkt.get('x', 0) * self.screen_w)))
        y = max(0, min(self.screen_h, int(pkt.get('y', 0) * self.screen_h)))
        d.write(e.EV_ABS, e.ABS_X, x)
        d.write(e.EV_ABS, e.ABS_Y, y)
        d.write(e.EV_ABS, e.ABS_PRESSURE,
                max(0, min(4095, int(pkt.get('p', 0) * 4095))))
        d.write(e.EV_ABS, e.ABS_TILT_X, max(-90, min(90, int(pkt.get('tx', 0) * 90))))
        d.write(e.EV_ABS, e.ABS_TILT_Y, max(-90, min(90, int(pkt.get('ty', 0) * 90))))
        d.write(e.EV_KEY, e.BTN_TOOL_PEN, 1 if pkt.get('rng') else 0)
        d.write(e.EV_KEY, e.BTN_TOUCH, 1 if pkt.get('tip') else 0)
        d.write(e.EV_KEY, e.BTN_STYLUS, 1 if pkt.get('btn') else 0)
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
# Host server
# ---------------------------------------------------------------------------
class HostServer:
    def __init__(self, target_ip, video_port, input_port, source,
                 fps, bitrate, enable_input, width=0):
        self.target_ip = target_ip
        self.video_port = video_port
        self.input_port = input_port
        self.source = source
        self.fps = fps
        self.bitrate = bitrate
        self.enable_input = enable_input
        self.width = width
        self.streamer = None
        self.input_server = None
        self.advertiser = None
        self.beacon = None
        self.capture = None
        self._stop = threading.Event()

    def start(self):
        logger.info("=" * 52)
        logger.info("  SP7 Monitor Host Server")
        logger.info(f"  target {self.target_ip}:{self.video_port}  "
                    f"input :{self.input_port}")
        logger.info("=" * 52)
        self.advertiser = ServiceAdvertiser(self.video_port)
        self.advertiser.start()
        # Keep announcing ourselves so the Surface can (re)discover us and
        # forward input back, even if its receiver restarts.
        self.beacon = DiscoveryBeacon(DISCOVERY_PORT)
        self.beacon.start()
        if self.enable_input:
            self.input_server = InputServer(self.input_port)
            self.input_server.start()
        self.streamer = VideoStreamer(self.target_ip, self.video_port,
                                      self.source, self.fps, self.bitrate,
                                      self.width)
        self.streamer.start()
        logger.info("Host server running — Ctrl+C to stop")

    def stop(self):
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
        if self.capture:
            self.capture.stop()

    def run(self):
        try:
            self.start()
            while not self._stop.is_set():
                self._stop.wait(1.0)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()


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
    parser.add_argument('--no-input', action='store_true',
                        help='Disable pen/touch input reception')
    parser.add_argument('--verbose', '-v', action='store_true')
    args = parser.parse_args()

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
    source = args.source or detect_capture_source()
    capture = None
    if source == 'wfrecorder':
        try:
            capture = WfRecorderCapture()
            source = capture.start()
            logger.info(f"Capture source: {source}")
        except Exception as e:
            logger.error(f"wf-recorder capture failed: {e}")
            logger.error("Falling back to a test pattern (pass --source to override)")
            source = 'videotestsrc is-live=true pattern=ball'
    elif source == 'portal':
        logger.info("Opening Wayland desktop capture via xdg-desktop-portal...")
        try:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            import portal_screencast
            fd, node = portal_screencast.open_screencast()
            source = portal_screencast.gst_source(fd, node)
            logger.info(f"Screencast source: {source}")
        except Exception as e:
            logger.error(f"Portal screencast failed: {e}")
            logger.error("Falling back to a test pattern (pass --source to override)")
            source = 'videotestsrc is-live=true pattern=ball'

    server = HostServer(target, args.video_port, args.input_port,
                        source, args.fps, args.bitrate, not args.no_input,
                        args.width)
    server.capture = capture
    signal.signal(signal.SIGINT, lambda *_: server.stop())
    signal.signal(signal.SIGTERM, lambda *_: server.stop())
    server.run()


if __name__ == '__main__':
    main()
