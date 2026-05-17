#!/usr/bin/env python3
"""
SP7 Wireless Monitor Receiver
=============================
Runs on the Surface Pro 7. Listens for an H.264 video stream from a host
PC and renders it full-screen via DRM/KMS, and forwards the Surface's
pen/touch input back to the host.

Design notes
------------
* The video path is a *listener* (`udpsrc`) — it needs no host address.
  The receiver therefore starts the video pipeline immediately and keeps
  it running whether or not a host is known.
* The host address is only needed to forward input *back*. It is taken
  from --host, then /etc/sp7-monitor/config.conf, then mDNS discovery.
  If none is found the receiver still runs (video-only); it never exits.
* Runs as a systemd service with no controlling terminal — there is no
  interactive prompting anywhere.

Usage: sp7-receiver.py [--host HOST] [--video-port N] [--input-port N]
                       [--no-input] [--width W] [--height H] [--verbose]
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

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%H:%M:%S',
)
logger = logging.getLogger('sp7-receiver')

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
SP7_WIDTH = 2736
SP7_HEIGHT = 1824

DEFAULT_VIDEO_PORT = 5004
DEFAULT_INPUT_PORT = 5005
DISCOVERY_PORT = 5006
CONFIG_PATH = "/etc/sp7-monitor/config.conf"

RTP_CAPS = ('application/x-rtp,media=video,encoding-name=H264,'
            'clock-rate=90000,payload=96')


def load_config(path: str = CONFIG_PATH) -> dict:
    """Parse the simple key=value config file. Missing file -> empty dict."""
    cfg: dict = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                key, _, val = line.partition('=')
                cfg[key.strip()] = val.strip()
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.warning(f"Could not read config {path}: {e}")
    return cfg


# ---------------------------------------------------------------------------
# mDNS host discovery (best-effort; no interactive prompting)
# ---------------------------------------------------------------------------
class HostDiscovery:
    """Discovers SP7 Monitor hosts on the local network via mDNS."""

    SERVICE_TYPE = "_sp7monitor._tcp.local."

    def discover(self, timeout: float = 3.0) -> list:
        """Return a list of {'address', 'port', 'name'} dicts, or []."""
        # Prefer avahi-browse if present
        try:
            result = subprocess.run(
                ['avahi-browse', '-rt', '-p', '_sp7monitor._tcp'],
                capture_output=True, text=True, timeout=timeout + 2,
            )
            if result.returncode == 0:
                hosts = self._parse_avahi(result.stdout)
                if hosts:
                    return hosts
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
        # Fallback: python-zeroconf
        try:
            return self._discover_zeroconf(timeout)
        except Exception as e:
            logger.debug(f"zeroconf discovery unavailable: {e}")
            return []

    @staticmethod
    def _parse_avahi(output: str) -> list:
        hosts = []
        for line in output.strip().splitlines():
            parts = line.split(';')
            if len(parts) >= 9 and parts[0] == '=':
                try:
                    hosts.append({'name': parts[3], 'address': parts[7],
                                  'port': int(parts[8])})
                except (ValueError, IndexError):
                    continue
        return hosts

    def _discover_zeroconf(self, timeout: float) -> list:
        from zeroconf import Zeroconf, ServiceBrowser, ServiceListener
        hosts = []

        class _L(ServiceListener):
            def add_service(self, zc, type_, name):
                info = zc.get_service_info(type_, name)
                if info and info.addresses:
                    hosts.append({
                        'name': name,
                        'address': socket.inet_ntoa(info.addresses[0]),
                        'port': info.port or DEFAULT_VIDEO_PORT,
                    })

            def update_service(self, *a):
                pass

            def remove_service(self, *a):
                pass

        zc = Zeroconf()
        ServiceBrowser(zc, self.SERVICE_TYPE, _L())
        time.sleep(timeout)
        zc.close()
        return hosts


# ---------------------------------------------------------------------------
# Video receiver (GStreamer)
# ---------------------------------------------------------------------------
class VideoReceiver:
    """Receives H.264/RTP over UDP, decodes, and renders via DRM/KMS."""

    def __init__(self, video_port: int, width: int, height: int):
        self.video_port = video_port
        self.width = width
        self.height = height
        self.pipeline: Optional[Gst.Pipeline] = None
        self.loop: Optional[GLib.MainLoop] = None
        self._loop_thread: Optional[threading.Thread] = None
        self._running = False
        self._need_restart = False

    @staticmethod
    def _pick_decoder() -> str:
        """H.264 decoder. Software avdec_h264 is the default: it is reliable
        and decodes straight to system-memory video/x-raw. The VA-API
        decoders (vah264dec) on the SP7's Gen11 iGPU fail caps negotiation
        into a non-VA sink ('not-negotiated'); software decode of 720p/1080p
        is cheap on the Surface's i5, so it is not worth the fragility."""
        if Gst.ElementFactory.find('avdec_h264'):
            logger.info("Using software H.264 decoder: avdec_h264")
            return "avdec_h264 max-threads=4 ! "
        logger.warning("avdec_h264 not found — falling back to vah264dec")
        # 'video/x-raw' forces the VA decoder to download to system memory
        # so the downstream videoconvert can accept the frames.
        return "vah264dec ! video/x-raw ! "

    @staticmethod
    def _pick_sink() -> str:
        """Choose the display sink. kmssink renders directly via DRM/KMS."""
        if Gst.ElementFactory.find('kmssink'):
            return "kmssink sync=false force-modesetting=true"
        logger.warning("kmssink not found — falling back to autovideosink")
        return "autovideosink sync=false"

    def _pick_scaler(self) -> str:
        """Scale the decoded frame up to the panel resolution. kmssink does
        NOT scale on this hardware — the frame handed to it must already be
        the panel size. vapostproc does the scale on the iGPU (VA-API), which
        costs almost no CPU; software videoscale (the fallback) was the
        receiver's CPU bottleneck — it caused the latency and glitches."""
        size = f"video/x-raw,width={self.width},height={self.height}"
        if Gst.ElementFactory.find('vapostproc'):
            logger.info("Scaling via vapostproc (VA-API hardware scaler)")
            return f"videoconvert ! vapostproc ! {size} ! "
        logger.warning("vapostproc not found — using software videoscale")
        return f"videoconvert ! videoscale ! {size} ! "

    def _build_pipeline(self) -> Gst.Pipeline:
        desc = (
            # buffer-size: a 4 MB kernel receive buffer rides out bursts so
            # the socket does not overflow (overflow = dropped RTP packets =
            # macroblock "glitch" artifacts).
            f'udpsrc port={self.video_port} buffer-size=4194304 '
            f'caps="{RTP_CAPS}" ! '
            # 150ms jitterbuffer: large enough that late/out-of-order packets
            # are still delivered (dropping H.264 packets corrupts the
            # picture), small enough not to dominate latency.
            'rtpjitterbuffer latency=150 ! '
            'rtph264depay ! h264parse ! '
            + self._pick_decoder() +
            # Leaky queue on *decoded* frames: if the display ever falls
            # behind, drop the oldest raw frame so latency cannot grow
            # without bound. Leaking raw frames is safe (a skipped frame);
            # leaking encoded frames would corrupt the stream.
            'queue leaky=downstream max-size-buffers=3 max-size-time=0 '
            'max-size-bytes=0 ! '
            # Hardware (VA-API) upscale to the panel resolution — software
            # upscaling every frame to 2736x1824 was the receiver's CPU
            # bottleneck: it starved the decoder, overflowed the UDP socket
            # (glitches) and accumulated latency.
            + self._pick_scaler()
            + self._pick_sink()
        )
        logger.info(f"Pipeline: {desc}")
        pipeline = Gst.parse_launch(desc)
        bus = pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_bus_message)
        return pipeline

    def _on_bus_message(self, bus, message):
        t = message.type
        if t == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            logger.error(f"GStreamer error: {err.message} ({debug})")
            self._need_restart = True   # manager thread will rebuild
        elif t == Gst.MessageType.WARNING:
            warn, _ = message.parse_warning()
            logger.warning(f"GStreamer warning: {warn.message}")
        elif t == Gst.MessageType.STATE_CHANGED and message.src == self.pipeline:
            old, new, _ = message.parse_state_changed()
            if new == Gst.State.PLAYING:
                logger.info("Video pipeline is PLAYING — listening for stream "
                            f"on UDP :{self.video_port}")

    def _manager(self) -> None:
        """Owns the pipeline lifecycle: brings it up and rebuilds it on
        failure. A transient failure (e.g. DRM contention) never kills the
        receiver — it just retries until the pipeline holds."""
        while self._running:
            if self._need_restart or self.pipeline is None:
                self._need_restart = False
                try:
                    if self.pipeline is not None:
                        self.pipeline.set_state(Gst.State.NULL)
                        time.sleep(2)
                    self.pipeline = self._build_pipeline()
                    ret = self.pipeline.set_state(Gst.State.PLAYING)
                    if ret == Gst.StateChangeReturn.FAILURE:
                        logger.warning("Pipeline set_state FAILURE — retry in 5s")
                        self._need_restart = True
                        time.sleep(5)
                    else:
                        logger.info(f"Pipeline bring-up: {ret.value_nick}")
                except Exception as e:
                    logger.error(f"Pipeline bring-up error: {e} — retry in 5s")
                    self._need_restart = True
                    time.sleep(5)
            time.sleep(1)

    def start(self) -> None:
        Gst.init(None)
        self._running = True
        self.loop = GLib.MainLoop()
        self._loop_thread = threading.Thread(target=self.loop.run, daemon=True)
        self._loop_thread.start()
        threading.Thread(target=self._manager, daemon=True).start()
        logger.info(f"Video receiver starting (UDP :{self.video_port})")

    def stop(self) -> None:
        self._running = False
        if self.loop:
            self.loop.quit()
        if self.pipeline:
            self.pipeline.set_state(Gst.State.NULL)
        logger.info("Video receiver stopped")


# ---------------------------------------------------------------------------
# Input forwarder (evdev capture -> network)
# ---------------------------------------------------------------------------
class InputForwarder:
    """Captures the Surface digitizer and forwards events to the host."""

    SEND_HZ = 120

    def __init__(self, host: str, input_port: int):
        self.host = host
        self.input_port = input_port
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._socket: Optional[socket.socket] = None

    @staticmethod
    def _find_input_device() -> Optional[str]:
        try:
            import evdev
        except ImportError:
            logger.error("python-evdev not installed — input disabled")
            return None
        candidates = []
        for path in evdev.list_devices():
            try:
                dev = evdev.InputDevice(path)
                caps = dev.capabilities()
                if evdev.ecodes.EV_ABS not in caps:
                    continue
                abs_codes = dict(caps[evdev.ecodes.EV_ABS])
                if (evdev.ecodes.ABS_X in abs_codes
                        and evdev.ecodes.ABS_Y in abs_codes):
                    name = dev.name.lower()
                    # Prefer the cooked iptsd virtual *pen*; the raw
                    # "Intel Touch Host Controller" is not what we want.
                    if 'host controller' in name:
                        score = 1
                    elif 'stylus' in name or 'pen' in name:
                        score = 4
                    elif 'iptsd' in name or 'touchscreen' in name:
                        score = 3
                    elif any(k in name for k in ('touch', 'surface',
                                                 'digitizer')):
                        score = 2
                    else:
                        score = 0
                    candidates.append((score, path, dev.name))
            except (PermissionError, OSError):
                continue
        if not candidates:
            logger.warning("No digitizer / touch device found")
            return None
        candidates.sort(reverse=True)
        _, path, name = candidates[0]
        logger.info(f"Input device: {name} ({path})")
        return path

    def _capture_loop(self, device_path: str) -> None:
        import evdev
        import msgpack
        try:
            device = evdev.InputDevice(device_path)
        except Exception as e:
            logger.error(f"Cannot open input device: {e}")
            return
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        logger.info(f"Forwarding input to {self.host}:{self.input_port}")

        absinfo = {}
        for code, info in device.capabilities().get(evdev.ecodes.EV_ABS, []):
            absinfo[code] = (info.min, info.max)

        state = {'x': 0.0, 'y': 0.0, 'p': 0.0, 'tx': 0.0, 'ty': 0.0,
                 'tip': False, 'rng': False, 'btn': 0}
        interval = 1.0 / self.SEND_HZ
        last = 0.0

        def norm(v, code, lo=0.0, hi=1.0):
            if code not in absinfo:
                return 0.0
            amin, amax = absinfo[code]
            if amax <= amin:
                return 0.0
            return max(lo, min(hi, lo + (v - amin) / (amax - amin) * (hi - lo)))

        while self._running:
            try:
                ev = device.read_one()
                if ev is None:
                    time.sleep(0.001)
                else:
                    if ev.type == evdev.ecodes.EV_ABS:
                        if ev.code == evdev.ecodes.ABS_X:
                            state['x'] = norm(ev.value, ev.code)
                        elif ev.code == evdev.ecodes.ABS_Y:
                            state['y'] = norm(ev.value, ev.code)
                        elif ev.code == evdev.ecodes.ABS_PRESSURE:
                            state['p'] = norm(ev.value, ev.code)
                        elif ev.code == evdev.ecodes.ABS_TILT_X:
                            state['tx'] = norm(ev.value, ev.code, -1.0, 1.0)
                        elif ev.code == evdev.ecodes.ABS_TILT_Y:
                            state['ty'] = norm(ev.value, ev.code, -1.0, 1.0)
                    elif ev.type == evdev.ecodes.EV_KEY:
                        if ev.code == evdev.ecodes.BTN_TOUCH:
                            state['tip'] = bool(ev.value)
                        elif ev.code == evdev.ecodes.BTN_TOOL_PEN:
                            state['rng'] = bool(ev.value)
                        elif ev.code == evdev.ecodes.BTN_STYLUS:
                            state['btn'] = 1 if ev.value else 0
                now = time.time()
                if now - last >= interval:
                    pkt = msgpack.packb({'t': now, **state}, use_bin_type=True)
                    self._socket.sendto(pkt, (self.host, self.input_port))
                    last = now
            except OSError as e:
                if self._running:
                    logger.error(f"Input socket error: {e}")
                time.sleep(0.5)
            except Exception as e:
                logger.error(f"Input capture error: {e}")
                time.sleep(0.1)
        logger.info("Input forwarder stopped")

    def start(self) -> bool:
        device_path = self._find_input_device()
        if not device_path:
            return False
        self._running = True
        self._thread = threading.Thread(
            target=self._capture_loop, args=(device_path,), daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._running = False
        if self._socket:
            try:
                self._socket.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Discovery responder — lets the host streamer find this Surface
# ---------------------------------------------------------------------------
class DiscoveryResponder:
    """Listens for host discovery broadcasts on UDP :DISCOVERY_PORT. On a
    probe it replies (so the host learns this Surface's address) and reports
    the host's address back to the receiver for input forwarding."""

    PROBE = b'SP7?'
    REPLY = b'SP7-MONITOR'

    def __init__(self, port: int, on_host):
        self.port = port
        self.on_host = on_host
        self._running = False
        self._sock: Optional[socket.socket] = None

    def start(self) -> None:
        self._running = True
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self) -> None:
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._sock.bind(('0.0.0.0', self.port))
            self._sock.settimeout(1.0)
        except Exception as e:
            logger.error(f"Discovery responder bind failed: {e}")
            return
        logger.info(f"Discovery responder listening on UDP :{self.port}")
        while self._running:
            try:
                data, addr = self._sock.recvfrom(256)
                if data.strip() == self.PROBE:
                    self._sock.sendto(self.REPLY, addr)
                    self.on_host(addr[0])
            except socket.timeout:
                continue
            except Exception as e:
                if self._running:
                    logger.debug(f"discovery: {e}")

    def stop(self) -> None:
        self._running = False
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Main receiver
# ---------------------------------------------------------------------------
class SP7Receiver:
    def __init__(self, host: Optional[str], video_port: int,
                 input_port: int, enable_input: bool = True):
        self.host = host
        self.video_port = video_port
        self.input_port = input_port
        self.enable_input = enable_input
        self.video: Optional[VideoReceiver] = None
        self.input_fwd: Optional[InputForwarder] = None
        self.disc: Optional[DiscoveryResponder] = None
        self._shutdown = threading.Event()

    def start(self) -> None:
        logger.info("=" * 52)
        logger.info("  SP7 Wireless Monitor Receiver")
        logger.info(f"  video :{self.video_port}  input :{self.input_port}  "
                    f"host: {self.host or '(awaiting discovery)'}")
        logger.info("=" * 52)

        # Video always starts — it just listens on a UDP port.
        self.video = VideoReceiver(self.video_port, SP7_WIDTH, SP7_HEIGHT)
        self.video.start()

        # Answer host discovery broadcasts; the host streamer finds us this
        # way, and we learn its address for input forwarding.
        self.disc = DiscoveryResponder(DISCOVERY_PORT, self._on_host)
        self.disc.start()

        if self.host:
            self._start_input(self.host)
        else:
            logger.info("Awaiting host discovery for input forwarding "
                        "(or set 'host=' in /etc/sp7-monitor/config.conf)")

    def _start_input(self, host_ip: str) -> None:
        """(Re)start input forwarding aimed at host_ip."""
        if not self.enable_input:
            return
        if self.input_fwd:
            self.input_fwd.stop()
        self.host = host_ip
        self.input_fwd = InputForwarder(host_ip, self.input_port)
        if not self.input_fwd.start():
            logger.warning("Input forwarding could not start")

    def _on_host(self, host_ip: str) -> None:
        """A host streamer announced itself via discovery."""
        if host_ip != self.host:
            logger.info(f"Host discovered: {host_ip} — forwarding input there")
            self._start_input(host_ip)

    def stop(self) -> None:
        logger.info("Shutting down receiver...")
        self._shutdown.set()
        if self.disc:
            self.disc.stop()
        if self.input_fwd:
            self.input_fwd.stop()
        if self.video:
            self.video.stop()

    def run(self) -> None:
        try:
            self.start()
            # Stay alive. The video pipeline self-recovers on errors;
            # the receiver never exits on its own.
            while not self._shutdown.is_set():
                self._shutdown.wait(2.0)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description='SP7 Wireless Monitor Receiver')
    parser.add_argument('--host', help='Host PC IP (for input forwarding)')
    parser.add_argument('--video-port', type=int)
    parser.add_argument('--input-port', type=int)
    parser.add_argument('--width', type=int)
    parser.add_argument('--height', type=int)
    parser.add_argument('--no-input', action='store_true',
                        help='Disable input forwarding (video only)')
    parser.add_argument('--verbose', '-v', action='store_true')
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    cfg = load_config()

    video_port = args.video_port or int(cfg.get('video_port', DEFAULT_VIDEO_PORT))
    input_port = args.input_port or int(cfg.get('input_port', DEFAULT_INPUT_PORT))

    global SP7_WIDTH, SP7_HEIGHT
    if args.width:
        SP7_WIDTH = args.width
    if args.height:
        SP7_HEIGHT = args.height

    # Resolve host for input forwarding: --host, then config, then mDNS.
    host = args.host or (cfg.get('host') or None)
    if not host:
        try:
            found = HostDiscovery().discover(timeout=3.0)
            if found:
                host = found[0]['address']
                logger.info(f"Auto-discovered host: {host}")
        except Exception as e:
            logger.warning(f"Host discovery failed: {e}")

    enable_input = not args.no_input

    receiver = SP7Receiver(host, video_port, input_port, enable_input)
    signal.signal(signal.SIGINT, lambda *_: receiver.stop())
    signal.signal(signal.SIGTERM, lambda *_: receiver.stop())
    receiver.run()


if __name__ == '__main__':
    main()
