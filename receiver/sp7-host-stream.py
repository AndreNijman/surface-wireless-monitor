#!/usr/bin/env python3
"""
SP7 Host Streaming Server
=========================
Runs on the host PC (Linux), captures the desktop display,
encodes to H.264 with hardware acceleration, and streams
to the Surface Pro 7 receiver over UDP/RTP.

Also receives pen/touch input events from the SP7 and injects
them into the local input system via uinput.

Usage: sp7-host-stream.py [--display DISPLAY] [--target TARGET_IP]
"""

import os
import sys
import time
import json
import socket
import signal
import struct
import logging
import argparse
import subprocess
import threading
import asyncio
from pathlib import Path
from typing import Optional, Callable

try:
    import gi
    gi.require_version('Gst', '1.0')
    from gi.repository import Gst, GLib
except ImportError:
    pass

try:
    import msgpack
except ImportError:
    print("ERROR: msgpack not installed. Run: pip3 install msgpack")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger('sp7-host')

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
DEFAULT_VIDEO_PORT = 5004
DEFAULT_INPUT_PORT = 5005
DEFAULT_FPS = 60
DEFAULT_BITRATE = 25000  # kbps

# ---------------------------------------------------------------------------
# Pipeline builders
# ---------------------------------------------------------------------------
def build_capture_pipeline(display: str, fps: int, bitrate: int,
                           target_ip: str, target_port: int) -> str:
    """
    Build a GStreamer pipeline for screen capture and H.264 streaming.
    Attempts hardware encoding, falls back to software.
    """
    # Detect available encoder
    encoder = detect_encoder()

    # Capture source (auto-detect best method)
    source = detect_capture_source(display)

    # Video scaling/filtering
    caps = f"video/x-raw,framerate={fps}/1"

    # Encoder-specific settings
    if encoder == 'vaapi':
        # Intel VAAPI hardware encoding
        encode_pipeline = (
            f"{source} ! {caps} ! "
            "vaapih264enc rate-control=cbr bitrate={bitrate} keyframe-period={gop} "
            "tune=low_latency num-slices=4 ! "
            "video/x-h264,profile=baseline,stream-format=byte-stream ! "
        )
    elif encoder == 'nvenc':
        # NVIDIA NVENC
        encode_pipeline = (
            f"{source} ! {caps} ! "
            "nvh264enc bitrate={bitrate} preset=low-latency-hq rc-mode=cbr "
            "gop-size={gop} bframes=0 ! "
            "video/x-h264,profile=baseline,stream-format=byte-stream ! "
        )
    elif encoder == 'x264':
        # Software x264
        encode_pipeline = (
            f"{source} ! {caps} ! "
            "videoconvert ! video/x-raw,format=I420 ! "
            "x264enc bitrate={kbps} speed-preset=ultrafast "
            "tune=zerolatency key-int-max={gop} vbv-buf-capacity=0 "
            "bframes=0 byte-stream=true ! "
            "video/x-h264,profile=baseline,stream-format=byte-stream ! "
        )
    else:
        # Software fallback with avenc
        encode_pipeline = (
            f"{source} ! {caps} ! "
            "videoconvert ! video/x-raw,format=I420 ! "
            "avenc_h264_omx bitrate={bitrate}000 gop-size={gop} ! "
        )

    # Complete pipeline with RTP packaging
    gop = fps * 2  # 2-second GOP
    kbps = bitrate // 1000

    pipeline = (
        encode_pipeline.format(bitrate=bitrate, kbps=kbps, gop=gop) +
        "h264parse config-interval=1 ! "
        f"rtph264pay pt=96 mtu=1400 ! "
        f"udpsink host={target_ip} port={target_port} sync=false buffer-size=524288"
    )

    logger.info(f"Using encoder: {encoder}")
    logger.info(f"Capture source: {source}")
    return pipeline


def detect_encoder() -> str:
    """Detect the best available H.264 encoder."""
    # Check VAAPI (Intel/AMD)
    try:
        result = subprocess.run(
            ['gst-inspect-1.0', 'vaapih264enc'],
            capture_output=True, timeout=5
        )
        if result.returncode == 0:
            return 'vaapi'
    except:
        pass

    # Check NVENC (NVIDIA)
    try:
        result = subprocess.run(
            ['gst-inspect-1.0', 'nvh264enc'],
            capture_output=True, timeout=5
        )
        if result.returncode == 0:
            return 'nvenc'
    except:
        pass

    # Fallback to x264
    return 'x264'


def detect_capture_source(display: str) -> str:
    """Detect the best screen capture source."""
    # Try pipewire (modern, Wayland-compatible)
    try:
        result = subprocess.run(
            ['gst-inspect-1.0', 'pipewiresrc'],
            capture_output=True, timeout=5
        )
        if result.returncode == 0:
            logger.info("Using PipeWire capture source")
            return "pipewiresrc"
    except:
        pass

    # Try ximagesrc (X11)
    try:
        result = subprocess.run(
            ['gst-inspect-1.0', 'ximagesrc'],
            capture_output=True, timeout=5
        )
        if result.returncode == 0:
            logger.info("Using X11 capture source")
            d = display or os.environ.get('DISPLAY', ':0')
            return f"ximagesrc display-name={d} use-damage=false show-pointer=true"
    except:
        pass

    # Try kms (DRM direct capture)
    try:
        result = subprocess.run(
            ['gst-inspect-1.0', 'kmssrc'],
            capture_output=True, timeout=5
        )
        if result.returncode == 0:
            logger.info("Using KMS/DRM capture source")
            return "kmssrc"
    except:
        pass

    # Ultimate fallback
    logger.warning("No optimized capture source found, using videotestsrc")
    return "videotestsrc is-live=true"


# ---------------------------------------------------------------------------
# Video Streamer
# ---------------------------------------------------------------------------
class VideoStreamer:
    """Captures desktop and streams H.264 to SP7 receiver."""

    def __init__(self, target_ip: str, video_port: int, display: str,
                 fps: int, bitrate: int):
        self.target_ip = target_ip
        self.video_port = video_port
        self.display = display
        self.fps = fps
        self.bitrate = bitrate
        self.pipeline: Optional[Gst.Pipeline] = None
        self.loop: Optional[GLib.MainLoop] = None
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def start(self):
        """Start the video stream."""
        Gst.init(None)

        pipeline_str = build_capture_pipeline(
            self.display, self.fps, self.bitrate,
            self.target_ip, self.video_port
        )

        logger.info(f"Pipeline: {pipeline_str}")

        self.pipeline = Gst.parse_launch(pipeline_str)
        if not self.pipeline:
            raise RuntimeError("Failed to create GStreamer pipeline")

        # Bus watch
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_bus_message)

        # Start playing
        ret = self.pipeline.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("Failed to start pipeline")

        self._running = True
        logger.info(f"Streaming to {self.target_ip}:{self.video_port}")

        # Run GLib main loop
        self.loop = GLib.MainLoop()
        self._thread = threading.Thread(target=self.loop.run, daemon=True)
        self._thread.start()

    def _on_bus_message(self, bus, message):
        """Handle GStreamer bus messages."""
        t = message.type
        if t == Gst.MessageType.EOS:
            logger.info("End of stream")
            self._running = False
        elif t == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            logger.error(f"GStreamer error: {err.message}")
            self._running = False
        elif t == Gst.MessageType.WARNING:
            warn, debug = message.parse_warning()
            logger.warning(f"GStreamer warning: {warn.message}")

    def stop(self):
        """Stop streaming."""
        self._running = False
        if self.loop:
            self.loop.quit()
        if self.pipeline:
            self.pipeline.set_state(Gst.State.NULL)
        logger.info("Video streamer stopped")


# ---------------------------------------------------------------------------
# Input Server (receives pen/touch from SP7)
# ---------------------------------------------------------------------------
class InputServer:
    """
    Receives pen/touch input events from Surface Pro 7
    and injects them via uinput.
    """

    def __init__(self, port: int, sp7_width: int, sp7_height: int):
        self.port = port
        self.sp7_width = sp7_width
        self.sp7_height = sp7_height
        self._running = False
        self._socket: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._uinput_dev = None

    def _setup_uinput(self):
        """Create a uinput virtual pen device."""
        try:
            import evdev
            from evdev import UInput, AbsInfo, ecodes as e

            # Get screen dimensions
            self.screen_w, self.screen_h = self._get_screen_size()

            # Define pen capabilities
            cap = {
                e.EV_KEY: [e.BTN_TOOL_PEN, e.BTN_TOOL_RUBBER, e.BTN_TOUCH,
                          e.BTN_STYLUS, e.BTN_STYLUS2],
                e.EV_ABS: [
                    (e.ABS_X, AbsInfo(0, 0, self.screen_w, 0, 0, self.screen_w)),
                    (e.ABS_Y, AbsInfo(0, 0, self.screen_h, 0, 0, self.screen_h)),
                    (e.ABS_PRESSURE, AbsInfo(0, 0, 4095, 0, 0, 4095)),
                    (e.ABS_TILT_X, AbsInfo(-90, -90, 90, 0, 0, 180)),
                    (e.ABS_TILT_Y, AbsInfo(-90, -90, 90, 0, 0, 180)),
                ],
            }

            self._uinput_dev = UInput(cap, name='SP7 Virtual Pen', version=0x3)
            logger.info(f"uinput device created: {self.screen_w}x{self.screen_h}")
            return True

        except ImportError:
            logger.error("python-evdev not installed, input injection disabled")
            return False
        except PermissionError:
            logger.error("Permission denied for uinput. Run as root or add user to 'input' group.")
            return False

    def _get_screen_size(self) -> tuple:
        """Get the current screen size."""
        try:
            import Xlib.display
            display = Xlib.display.Display()
            screen = display.screen()
            return screen.width_in_pixels, screen.height_in_pixels
        except:
            try:
                result = subprocess.run(
                    ['xrandr'], capture_output=True, text=True, timeout=5
                )
                for line in result.stdout.split('\n'):
                    if '*' in line:
                        parts = line.split()[0].split('x')
                        return int(parts[0]), int(parts[1])
            except:
                pass
        return 1920, 1080  # Default fallback

    def _receive_loop(self):
        """Receive input events and inject."""
        try:
            self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._socket.bind(('0.0.0.0', self.port))
            self._socket.settimeout(1.0)
        except Exception as e:
            logger.error(f"Failed to bind socket: {e}")
            return

        logger.info(f"Input server listening on port {self.port}")

        packets_received = 0
        last_stats = time.time()

        while self._running:
            try:
                data, addr = self._socket.recvfrom(1024)
                packet = msgpack.unpackb(data, raw=False)

                if self._uinput_dev:
                    self._inject_event(packet)

                packets_received += 1

                # Stats
                now = time.time()
                if now - last_stats >= 10.0:
                    logger.info(f"Input: {packets_received} packets/10s")
                    packets_received = 0
                    last_stats = now

            except socket.timeout:
                continue
            except Exception as e:
                if self._running:
                    logger.error(f"Input receive error: {e}")

        logger.info("Input server stopped")

    def _inject_event(self, packet: dict):
        """Inject an input event via uinput."""
        try:
            from evdev import ecodes as e

            dev = self._uinput_dev
            x = int(packet.get('x', 0) * self.screen_w)
            y = int(packet.get('y', 0) * self.screen_h)
            pressure = int(packet.get('p', 0) * 4095)
            tilt_x = int(packet.get('tx', 0) * 90)
            tilt_y = int(packet.get('ty', 0) * 90)
            touching = packet.get('tip', False)
            in_range = packet.get('rng', False)
            tool = packet.get('tool', 'none')
            btn1 = packet.get('btn1', False)
            btn2 = packet.get('btn2', False)

            # Write absolute axis events
            dev.write(e.EV_ABS, e.ABS_X, max(0, min(self.screen_w, x)))
            dev.write(e.EV_ABS, e.ABS_Y, max(0, min(self.screen_h, y)))
            dev.write(e.EV_ABS, e.ABS_PRESSURE, max(0, min(4095, pressure)))
            dev.write(e.EV_ABS, e.ABS_TILT_X, max(-90, min(90, tilt_x)))
            dev.write(e.EV_ABS, e.ABS_TILT_Y, max(-90, min(90, tilt_y)))

            # Write key events
            dev.write(e.EV_KEY, e.BTN_TOOL_PEN, 1 if in_range and tool == 'pen' else 0)
            dev.write(e.EV_KEY, e.BTN_TOOL_RUBBER, 1 if in_range and tool == 'eraser' else 0)
            dev.write(e.EV_KEY, e.BTN_TOUCH, 1 if touching else 0)
            dev.write(e.EV_KEY, e.BTN_STYLUS, 1 if btn1 else 0)
            dev.write(e.EV_KEY, e.BTN_STYLUS2, 1 if btn2 else 0)

            # Sync
            dev.write(e.EV_SYN, e.SYN_REPORT, 0)
            dev.syn()

        except Exception as e:
            logger.debug(f"Injection error: {e}")

    def start(self):
        """Start the input server."""
        self._running = True
        self._setup_uinput()

        self._thread = threading.Thread(target=self._receive_loop, daemon=True)
        self._thread.start()
        logger.info("Input server started")

    def stop(self):
        """Stop the input server."""
        self._running = False
        if self._socket:
            try:
                self._socket.close()
            except:
                pass
        if self._thread:
            self._thread.join(timeout=3.0)
        if self._uinput_dev:
            try:
                self._uinput_dev.close()
            except:
                pass
        logger.info("Input server stopped")


# ---------------------------------------------------------------------------
# mDNS Service Advertisement
# ---------------------------------------------------------------------------
class ServiceAdvertiser:
    """Advertise the SP7 monitor host service via mDNS/DNS-SD."""

    def __init__(self, port: int):
        self.port = port
        self._running = False

    def start(self):
        """Start advertising via avahi-publish or python-zeroconf."""
        self._running = True

        # Try avahi-publish first
        try:
            subprocess.Popen(
                ['avahi-publish-service', 'sp7-monitor-host', '_sp7monitor._tcp',
                 str(self.port), 'version=1.0', 'proto=udp'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            logger.info("mDNS service advertised via avahi")
            return
        except FileNotFoundError:
            pass

        # Fallback: try python-zeroconf
        try:
            from zeroconf import Zeroconf, ServiceInfo
            import socket

            hostname = socket.gethostname()
            ip = socket.gethostbyname(hostname)

            info = ServiceInfo(
                "_sp7monitor._tcp.local.",
                f"{hostname}._sp7monitor._tcp.local.",
                addresses=[socket.inet_aton(ip)],
                port=self.port,
                properties={"version": "1.0", "proto": "udp"}
            )
            self._zeroconf = Zeroconf()
            self._zeroconf.register_service(info)
            logger.info(f"mDNS service advertised via zeroconf ({ip})")
        except Exception as e:
            logger.warning(f"Could not advertise mDNS service: {e}")

    def stop(self):
        self._running = False
        if hasattr(self, '_zeroconf'):
            try:
                self._zeroconf.unregister_all_services()
                self._zeroconf.close()
            except:
                pass


# ---------------------------------------------------------------------------
# Main Host Server
# ---------------------------------------------------------------------------
class HostServer:
    """Main host server coordinating video streaming and input reception."""

    def __init__(self, target_ip: str, video_port: int, input_port: int,
                 display: str, fps: int, bitrate: int):
        self.target_ip = target_ip
        self.video_port = video_port
        self.input_port = input_port
        self.display = display
        self.fps = fps
        self.bitrate = bitrate

        self.streamer: Optional[VideoStreamer] = None
        self.input_server: Optional[InputServer] = None
        self.advertiser: Optional[ServiceAdvertiser] = None

    def start(self):
        """Start all host services."""
        logger.info("=" * 50)
        logger.info("  SP7 Monitor Host Server Starting")
        logger.info("=" * 50)
        logger.info(f"Target: {self.target_ip}:{self.video_port}")
        logger.info(f"Input port: {self.input_port}")
        logger.info(f"Display: {self.display}")
        logger.info(f"FPS: {self.fps}, Bitrate: {self.bitrate}kbps")

        # Advertise service
        self.advertiser = ServiceAdvertiser(self.video_port)
        self.advertiser.start()

        # Start input server
        self.input_server = InputServer(
            self.input_port,
            sp7_width=2736, sp7_height=1824
        )
        self.input_server.start()

        # Start video stream
        self.streamer = VideoStreamer(
            self.target_ip, self.video_port,
            self.display, self.fps, self.bitrate
        )
        self.streamer.start()

        logger.info("=" * 50)
        logger.info("  Host server running. Press Ctrl+C to stop.")
        logger.info("=" * 50)

    def stop(self):
        """Stop all services."""
        logger.info("Shutting down host server...")
        if self.streamer:
            self.streamer.stop()
        if self.input_server:
            self.input_server.stop()
        if self.advertiser:
            self.advertiser.stop()
        logger.info("Host server stopped")

    def run(self):
        """Run until interrupted."""
        try:
            self.start()
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            logger.info("Interrupted")
        finally:
            self.stop()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description='SP7 Monitor Host - Stream display and receive pen input',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --target 192.168.1.50          Stream to SP7 at given IP
  %(prog)s --target 192.168.1.50 --fps 30  30 FPS for smoother performance
  %(prog)s --target 192.168.1.50 -d :0    Capture display :0

Prerequisites:
  - GStreamer with capture source (ximagesrc/pipewiresrc)
  - H.264 encoder (vaapih264enc/nvh264enc/x264enc)
  - python-evdev for input injection
  - msgpack for network serialization
        """
    )
    parser.add_argument('--target', '-t', required=True,
                        help='SP7 receiver IP address')
    parser.add_argument('--video-port', type=int, default=DEFAULT_VIDEO_PORT)
    parser.add_argument('--input-port', type=int, default=DEFAULT_INPUT_PORT)
    parser.add_argument('--display', '-d', default=':0',
                        help='X11 display to capture (default: :0)')
    parser.add_argument('--fps', type=int, default=DEFAULT_FPS,
                        help=f'Frames per second (default: {DEFAULT_FPS})')
    parser.add_argument('--bitrate', type=int, default=DEFAULT_BITRATE,
                        help=f'Bitrate in kbps (default: {DEFAULT_BITRATE})')
    parser.add_argument('--no-input', action='store_true',
                        help='Disable input reception')
    parser.add_argument('--verbose', '-v', action='store_true')

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    Gst.init(None)

    server = HostServer(
        target_ip=args.target,
        video_port=args.video_port,
        input_port=args.input_port,
        display=args.display,
        fps=args.fps,
        bitrate=args.bitrate
    )

    signal.signal(signal.SIGINT, lambda s, f: server.stop())
    signal.signal(signal.SIGTERM, lambda s, f: server.stop())

    server.run()


if __name__ == '__main__':
    main()
