#!/usr/bin/env python3
"""
SP7 Wireless Monitor Receiver
=============================
Runs on the Surface Pro 7, receives video stream from host PC,
displays it on the built-in screen, and captures touch/pen input
for forwarding back to the host.

Usage: sp7-receiver.py [--host HOST] [--port PORT]

Auto-discovers host via mDNS if not specified.
"""

import os
import sys
import time
import json
import socket
import signal
import asyncio
import logging
import argparse
import subprocess
import threading
from pathlib import Path
from dataclasses import dataclass, asdict
from typing import Optional, Callable

import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger('sp7-receiver')

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
SP7_WIDTH = 2736
SP7_HEIGHT = 1824
SP7_ASPECT = SP7_WIDTH / SP7_HEIGHT

DEFAULT_VIDEO_PORT = 5004
DEFAULT_INPUT_PORT = 5005
DEFAULT_DISCOVERY_PORT = 5353

# GStreamer low-latency pipeline
# Receives H.264 RTP over UDP, hardware decodes with VAAPI, renders via DRM/KMS
GST_PIPELINE = (
    "udpsrc port={video_port} caps=\"application/x-rtp,media=video,encoding-name=H264,payload=96\" ! "
    "rtpjitterbuffer latency=20 drop-on-latency=true ! "
    "rtph264depay ! h264parse ! "
    "vaapih264dec low-latency=1 ! "
    "videoconvert ! "
    "videoscale ! "
    "video/x-raw,width={width},height={height} ! "
    "kmssink sync=false force-modesetting=true "
)

# Fallback pipeline (no VAAPI) - software decode
GST_PIPELINE_FALLBACK = (
    "udpsrc port={video_port} caps=\"application/x-rtp,media=video,encoding-name=H264,payload=96\" ! "
    "rtpjitterbuffer latency=20 drop-on-latency=true ! "
    "rtph264depay ! h264parse ! "
    "avdec_h264 max-threads=4 output-corrupt=false ! "
    "videoconvert ! "
    "videoscale ! "
    "video/x-raw,width={width},height={height} ! "
    "kmssink sync=false force-modesetting=true "
)

# X11-based pipeline (for development/testing)
GST_PIPELINE_X11 = (
    "udpsrc port={video_port} caps=\"application/x-rtp,media=video,encoding-name=H264,payload=96\" ! "
    "rtpjitterbuffer latency=20 drop-on-latency=true ! "
    "rtph264depay ! h264parse ! "
    "vaapih264dec low-latency=1 ! "
    "videoconvert ! "
    "videoscale ! "
    "video/x-raw,width={width},height={height} ! "
    "xvimagesink sync=false "
)


# ---------------------------------------------------------------------------
# mDNS Service Discovery
# ---------------------------------------------------------------------------
class HostDiscovery:
    """Discovers SP7 Monitor hosts on the local network via mDNS."""

    SERVICE_TYPE = "_sp7monitor._tcp.local."

    def __init__(self):
        self.hosts: list[dict] = []
        self._running = False

    def discover(self, timeout: float = 5.0) -> list[dict]:
        """
        Discover available hosts via mDNS/DNS-SD.
        Falls back to Avahi command-line tools if zeroconf is not available.
        """
        hosts = []

        # Try avahi-browse (Debian systems)
        try:
            result = subprocess.run(
                ['avahi-browse', '-rt', '_sp7monitor._tcp', '-p', '-t'],
                capture_output=True, text=True, timeout=timeout
            )
            if result.returncode == 0:
                hosts = self._parse_avahi_output(result.stdout)
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

        # Fallback: try Python zeroconf
        if not hosts:
            try:
                hosts = self._discover_zeroconf(timeout)
            except Exception as e:
                logger.warning(f"zeroconf discovery failed: {e}")

        return hosts

    def _parse_avahi_output(self, output: str) -> list[dict]:
        """Parse avahi-browse -p output."""
        hosts = []
        current = {}
        for line in output.strip().split('\n'):
            parts = line.split(';')
            if len(parts) < 4:
                continue
            if parts[0] == '=':
                iface, proto, name, svc_type, domain, host, addr, port, txt = parts[1:10]
                hosts.append({
                    'name': name,
                    'address': addr,
                    'host': host.rstrip('.'),
                    'port': int(port),
                    'interface': iface,
                })
        return hosts

    def _discover_zeroconf(self, timeout: float) -> list[dict]:
        """Discover using Python zeroconf library."""
        try:
            from zeroconf import Zeroconf, ServiceBrowser, ServiceListener
        except ImportError:
            return []

        hosts = []

        class DiscoveryListener(ServiceListener):
            def add_service(self, zc, type_, name):
                info = zc.get_service_info(type_, name)
                if info and info.addresses:
                    hosts.append({
                        'name': name,
                        'address': socket.inet_ntoa(info.addresses[0]),
                        'host': info.server.rstrip('.') if info.server else '',
                        'port': info.port,
                    })

        zeroconf = Zeroconf()
        listener = DiscoveryListener()
        browser = ServiceBrowser(zeroconf, self.SERVICE_TYPE, listener)
        time.sleep(timeout)
        zeroconf.close()
        return hosts

    def get_host_interactive(self) -> Optional[dict]:
        """Interactively select a host or enter IP manually."""
        print("\n" + "=" * 50)
        print("  SP7 Wireless Monitor - Host Discovery")
        print("=" * 50)

        hosts = self.discover(timeout=3.0)

        if hosts:
            print(f"\nFound {len(hosts)} host(s):")
            for i, h in enumerate(hosts, 1):
                print(f"  [{i}] {h['name']} at {h['address']}:{h['port']}")
        else:
            print("\nNo hosts discovered automatically.")

        print(f"  [0] Enter host IP address manually")
        print("=" * 50)

        try:
            choice = input("\nSelect host [0]: ").strip()
            if not choice:
                choice = '0'
            choice = int(choice)
        except (ValueError, KeyboardInterrupt, EOFError):
            choice = 0

        if choice == 0:
            try:
                ip = input("Enter host IP address: ").strip()
                port = input(f"Enter video port [{DEFAULT_VIDEO_PORT}]: ").strip()
                port = int(port) if port else DEFAULT_VIDEO_PORT
                return {'address': ip, 'port': port, 'name': 'manual'}
            except (ValueError, KeyboardInterrupt, EOFError):
                logger.error("Invalid input")
                return None
        elif 1 <= choice <= len(hosts):
            return hosts[choice - 1]
        else:
            return None


# ---------------------------------------------------------------------------
# Video Receiver (GStreamer)
# ---------------------------------------------------------------------------
class VideoReceiver:
    """
    Receives H.264 video stream over UDP/RTP,
    hardware decodes with VAAPI, renders to display.
    """

    def __init__(self, host: str, video_port: int, width: int, height: int):
        self.host = host
        self.video_port = video_port
        self.width = width
        self.height = height
        self.pipeline: Optional[Gst.Pipeline] = None
        self.loop: Optional[GLib.MainLoop] = None
        self._running = False

    def build_pipeline(self, try_hardware: bool = True) -> Gst.Pipeline:
        """Build the GStreamer pipeline."""
        format_args = {
            'video_port': self.video_port,
            'width': self.width,
            'height': self.height,
        }

        # Try VAAPI hardware decode first
        if try_hardware:
            pipeline_str = GST_PIPELINE.format(**format_args)
            logger.info("Using VAAPI hardware decode pipeline")
        else:
            pipeline_str = GST_PIPELINE_FALLBACK.format(**format_args)
            logger.info("Using software decode fallback pipeline")

        pipeline = Gst.parse_launch(pipeline_str)
        if not pipeline:
            raise RuntimeError("Failed to create GStreamer pipeline")

        # Add bus watch for errors
        bus = pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_bus_message)

        return pipeline

    def _on_bus_message(self, bus, message):
        """Handle GStreamer bus messages."""
        t = message.type
        if t == Gst.MessageType.EOS:
            logger.info("End of stream")
            self._running = False
        elif t == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            logger.error(f"GStreamer error: {err.message}")
            if debug:
                logger.debug(f"Debug: {debug}")
            self._running = False
        elif t == Gst.MessageType.WARNING:
            warn, debug = message.parse_warning()
            logger.warning(f"GStreamer warning: {warn.message}")
        elif t == Gst.MessageType.STATE_CHANGED:
            if message.src == self.pipeline:
                old_state, new_state, pending = message.parse_state_changed()
                logger.debug(f"Pipeline state: {old_state.value_nick} -> {new_state.value_nick}")

    def start(self) -> None:
        """Start video reception."""
        Gst.init(None)

        # First try: VAAPI hardware decode
        try:
            self.pipeline = self.build_pipeline(try_hardware=True)
        except Exception as e:
            logger.warning(f"VAAPI pipeline failed: {e}, trying software fallback")
            self.pipeline = self.build_pipeline(try_hardware=False)

        # Set pipeline to playing
        ret = self.pipeline.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("Failed to start pipeline")

        self._running = True
        logger.info(f"Video receiver started on port {self.video_port}")
        logger.info(f"Waiting for stream from {self.host}...")

        # Run GLib main loop in background thread
        self.loop = GLib.MainLoop()
        self._loop_thread = threading.Thread(target=self.loop.run, daemon=True)
        self._loop_thread.start()

    def stop(self) -> None:
        """Stop video reception."""
        self._running = False
        if self.loop:
            self.loop.quit()
        if self.pipeline:
            self.pipeline.set_state(Gst.State.NULL)
        logger.info("Video receiver stopped")

    @property
    def is_running(self) -> bool:
        return self._running


# ---------------------------------------------------------------------------
# Input Forwarder (evdev capture -> network)
# ---------------------------------------------------------------------------
class InputForwarder:
    """
    Captures touch/pen events from Surface Pro 7 digitizer
    and forwards them to the host PC.
    """

    def __init__(self, host: str, input_port: int):
        self.host = host
        self.input_port = input_port
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._socket: Optional[socket.socket] = None

    def _find_input_device(self) -> Optional[str]:
        """Find the Surface pen/touch input device."""
        try:
            import evdev
        except ImportError:
            logger.error("python-evdev not installed")
            return None

        # Look for Surface-specific devices
        for path in evdev.list_devices():
            try:
                dev = evdev.InputDevice(path)
                name_lower = dev.name.lower()
                if any(k in name_lower for k in ['ipts', 'pen', 'touch', 'stylus', 'surface']):
                    caps = dev.capabilities()
                    # Check for absolute positioning (digitizer)
                    if evdev.ecodes.EV_ABS in caps:
                        logger.info(f"Found input device: {dev.name} at {path}")
                        return path
            except (PermissionError, OSError):
                continue

        # Fallback: any device with ABS_X/ABS_Y
        for path in evdev.list_devices():
            try:
                dev = evdev.InputDevice(path)
                caps = dev.capabilities()
                if evdev.ecodes.EV_ABS in caps:
                    abs_caps = caps[evdev.ecodes.EV_ABS]
                    if (evdev.ecodes.ABS_X in abs_caps and
                        evdev.ecodes.ABS_Y in abs_caps):
                        logger.info(f"Found input device (fallback): {dev.name} at {path}")
                        return path
            except (PermissionError, OSError):
                continue

        logger.warning("No suitable input device found")
        return None

    def _capture_loop(self, device_path: str) -> None:
        """Main capture and forward loop."""
        try:
            import evdev
            import msgpack
        except ImportError as e:
            logger.error(f"Missing dependency: {e}")
            return

        try:
            device = evdev.InputDevice(device_path)
            device.grab()  # Exclusive access
            logger.info(f"Grabbed input device: {device.name}")
        except Exception as e:
            logger.error(f"Failed to grab device: {e}")
            return

        # Connect to host
        try:
            self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._socket.settimeout(2.0)
            logger.info(f"Input forwarding to {self.host}:{self.input_port}")
        except Exception as e:
            logger.error(f"Failed to create socket: {e}")
            return

        # Get device capabilities for coordinate mapping
        absinfo = {}
        caps = device.capabilities()
        if evdev.ecodes.EV_ABS in caps:
            for code, info in caps[evdev.ecodes.EV_ABS]:
                absinfo[code] = {
                    'min': info.min,
                    'max': info.max,
                    'res': info.resolution if hasattr(info, 'resolution') else None,
                }

        # Normalize and forward events
        pen_state = {
            'x': 0.0, 'y': 0.0,
            'pressure': 0.0,
            'tilt_x': 0.0, 'tilt_y': 0.0,
            'buttons': 0,
            'in_range': False,
            'touching': False,
        }

        last_send_time = 0
        SEND_INTERVAL = 1.0 / 120  # 120 Hz max send rate

        while self._running:
            try:
                # Read events with timeout
                events = device.read_one()
                if events is None:
                    time.sleep(0.001)
                    continue

                # Process event
                self._process_event(events, pen_state, absinfo)

                # Send at throttled rate
                now = time.time()
                if now - last_send_time >= SEND_INTERVAL:
                    packet = self._build_packet(pen_state)
                    data = msgpack.packb(packet, use_bin_type=True)
                    self._socket.sendto(data, (self.host, self.input_port))
                    last_send_time = now

            except OSError as e:
                if self._running:
                    logger.error(f"Socket error: {e}")
                break
            except Exception as e:
                logger.error(f"Input capture error: {e}")
                time.sleep(0.1)

        try:
            device.ungrab()
        except:
            pass
        logger.info("Input forwarder stopped")

    def _process_event(self, event, state: dict, absinfo: dict) -> None:
        """Process a single evdev event."""
        import evdev

        if event.type == evdev.ecodes.EV_ABS:
            if event.code == evdev.ecodes.ABS_X:
                state['x'] = self._normalize(event.value, absinfo.get(evdev.ecodes.ABS_X, {}))
            elif event.code == evdev.ecodes.ABS_Y:
                state['y'] = self._normalize(event.value, absinfo.get(evdev.ecodes.ABS_Y, {}))
            elif event.code == evdev.ecodes.ABS_PRESSURE:
                state['pressure'] = self._normalize(event.value, absinfo.get(evdev.ecodes.ABS_PRESSURE, {}))
            elif event.code == evdev.ecodes.ABS_TILT_X:
                state['tilt_x'] = self._normalize_tilt(event.value, absinfo.get(evdev.ecodes.ABS_TILT_X, {}))
            elif event.code == evdev.ecodes.ABS_TILT_Y:
                state['tilt_y'] = self._normalize_tilt(event.value, absinfo.get(evdev.ecodes.ABS_TILT_Y, {}))

        elif event.type == evdev.ecodes.EV_KEY:
            if event.code == evdev.ecodes.BTN_TOUCH:
                state['touching'] = bool(event.value)
            elif event.code == evdev.ecodes.BTN_TOOL_PEN:
                state['in_range'] = bool(event.value)
            elif event.code == evdev.ecodes.BTN_TOOL_RUBBER:
                state['buttons'] = 1 if event.value else 0

    def _normalize(self, value: int, info: dict) -> float:
        """Normalize to [0, 1] range."""
        min_val = info.get('min', 0)
        max_val = info.get('max', 1)
        if max_val <= min_val:
            return 0.0
        return max(0.0, min(1.0, (value - min_val) / (max_val - min_val)))

    def _normalize_tilt(self, value: int, info: dict) -> float:
        """Normalize tilt to [-1, 1] range."""
        min_val = info.get('min', -90)
        max_val = info.get('max', 90)
        if max_val <= min_val:
            return 0.0
        return max(-1.0, min(1.0, 2.0 * (value - min_val) / (max_val - min_val) - 1.0))

    def _build_packet(self, state: dict) -> dict:
        """Build network packet from pen state."""
        return {
            't': time.time(),
            'x': state['x'],
            'y': state['y'],
            'p': round(state['pressure'], 4),
            'tx': round(state['tilt_x'], 4),
            'ty': round(state['tilt_y'], 4),
            'tip': state['touching'],
            'rng': state['in_range'],
            'btn': state['buttons'],
        }

    def start(self) -> None:
        """Start input capture and forwarding."""
        self._running = True

        device_path = self._find_input_device()
        if not device_path:
            logger.error("No input device found, input forwarding disabled")
            return

        self._thread = threading.Thread(
            target=self._capture_loop,
            args=(device_path,),
            daemon=True
        )
        self._thread.start()
        logger.info("Input forwarder started")

    def stop(self) -> None:
        """Stop input forwarding."""
        self._running = False
        if self._socket:
            try:
                self._socket.close()
            except:
                pass
        if self._thread:
            self._thread.join(timeout=2.0)
        logger.info("Input forwarder stopped")


# ---------------------------------------------------------------------------
# Main Receiver
# ---------------------------------------------------------------------------
class SP7Receiver:
    """
    Main SP7 Wireless Monitor receiver.
    Coordinates video reception and input forwarding.
    """

    def __init__(self, host: str, video_port: int, input_port: int):
        self.host = host
        self.video_port = video_port
        self.input_port = input_port

        self.video: Optional[VideoReceiver] = None
        self.input_fwd: Optional[InputForwarder] = None
        self._shutdown_event = threading.Event()

    def setup_display(self) -> None:
        """Configure display for optimal performance."""
        logger.info("Configuring display...")

        # Disable screen blanking and DPMS
        for cmd in [
            ['xset', 's', 'off'],
            ['xset', '-dpms'],
            ['xset', 's', 'noblank'],
        ]:
            try:
                subprocess.run(cmd, capture_output=True, timeout=5)
            except:
                pass

        # Try to set native resolution via KMS/DRM
        try:
            result = subprocess.run(
                ['xrandr'], capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0 and 'connected' in result.stdout:
                logger.info("Display connected via X11/KMS")
        except:
            pass

    def start(self) -> None:
        """Start all receiver components."""
        logger.info("=" * 50)
        logger.info("  SP7 Wireless Monitor Receiver Starting")
        logger.info("=" * 50)
        logger.info(f"Host: {self.host}")
        logger.info(f"Video port: {self.video_port}")
        logger.info(f"Input port: {self.input_port}")

        # Setup display
        self.setup_display()

        # Start video receiver
        self.video = VideoReceiver(
            self.host, self.video_port, SP7_WIDTH, SP7_HEIGHT
        )
        try:
            self.video.start()
        except Exception as e:
            logger.error(f"Failed to start video receiver: {e}")
            raise

        # Start input forwarding
        self.input_fwd = InputForwarder(self.host, self.input_port)
        self.input_fwd.start()

        logger.info("=" * 50)
        logger.info("  Receiver running. Press Ctrl+C to stop.")
        logger.info("=" * 50)

    def stop(self) -> None:
        """Stop all components gracefully."""
        logger.info("Shutting down receiver...")
        self._shutdown_event.set()
        if self.input_fwd:
            self.input_fwd.stop()
        if self.video:
            self.video.stop()
        logger.info("Receiver stopped")

    def run(self) -> None:
        """Main run loop."""
        try:
            self.start()
            # Wait for shutdown signal
            while not self._shutdown_event.is_set():
                if self.video and not self.video.is_running:
                    logger.warning("Video stream ended, restarting in 3s...")
                    time.sleep(3)
                    try:
                        self.video.start()
                    except Exception as e:
                        logger.error(f"Restart failed: {e}")
                self._shutdown_event.wait(1.0)
        except KeyboardInterrupt:
            logger.info("Interrupted by user")
        finally:
            self.stop()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description='SP7 Wireless Monitor Receiver',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                           # Auto-discover host
  %(prog)s --host 192.168.1.100      # Direct connect to host
  %(prog)s --host 192.168.1.100 --video-port 5004 --input-port 5005
        """
    )
    parser.add_argument('--host', help='Host PC IP address (auto-discover if omitted)')
    parser.add_argument('--video-port', type=int, default=DEFAULT_VIDEO_PORT,
                        help=f'Video RTP port (default: {DEFAULT_VIDEO_PORT})')
    parser.add_argument('--input-port', type=int, default=DEFAULT_INPUT_PORT,
                        help=f'Input UDP port (default: {DEFAULT_INPUT_PORT})')
    parser.add_argument('--no-input', action='store_true',
                        help='Disable input forwarding')
    parser.add_argument('--verbose', '-v', action='store_true',
                        help='Enable verbose logging')
    parser.add_argument('--width', type=int, default=SP7_WIDTH,
                        help=f'Display width (default: {SP7_WIDTH})')
    parser.add_argument('--height', type=int, default=SP7_HEIGHT,
                        help=f'Display height (default: {SP7_HEIGHT})')

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # Determine host
    host = args.host
    if not host:
        discovery = HostDiscovery()
        result = discovery.get_host_interactive()
        if result:
            host = result['address']
            if 'port' in result and result['port'] != DEFAULT_VIDEO_PORT:
                args.video_port = result['port']
        if not host:
            print("No host selected. Exiting.")
            sys.exit(1)

    # Update SP7 dimensions if overridden
    global SP7_WIDTH, SP7_HEIGHT
    SP7_WIDTH = args.width
    SP7_HEIGHT = args.height

    # Create and run receiver
    receiver = SP7Receiver(host, args.video_port, args.input_port)

    # Handle signals
    signal.signal(signal.SIGINT, lambda s, f: receiver.stop())
    signal.signal(signal.SIGTERM, lambda s, f: receiver.stop())

    receiver.run()


if __name__ == '__main__':
    main()
