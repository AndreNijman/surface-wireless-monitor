#!/usr/bin/env python3
"""
SP7 Input Client
================
Captures touch/pen events from Surface Pro 7 digitizer via evdev
and forwards them to the host PC over UDP using MessagePack serialization.

This is a standalone input forwarding client that can run independently
of the video receiver.

Usage: sp7-input-client.py --host <HOST_IP> [--port PORT]
"""

import os
import sys
import time
import socket
import signal
import struct
import logging
import argparse
import threading
from pathlib import Path
from typing import Optional, Dict, Any

try:
    import evdev
    from evdev import ecodes, InputDevice, list_devices
except ImportError:
    print("ERROR: python-evdev not installed. Run: pip3 install evdev")
    sys.exit(1)

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
logger = logging.getLogger('sp7-input')

# ---------------------------------------------------------------------------
# Default configuration
# ---------------------------------------------------------------------------
DEFAULT_HOST = "192.168.1.100"
DEFAULT_PORT = 5005
SEND_RATE_HZ = 120  # Max event send rate


# ---------------------------------------------------------------------------
# Input Device Scanner
# ---------------------------------------------------------------------------
def find_surface_input_devices() -> list[dict]:
    """
    Scan for Surface Pro 7 input devices (touch and pen).
    Returns list of device info dicts.
    """
    devices = []

    for path in list_devices():
        try:
            dev = InputDevice(path)
            name_lower = dev.name.lower()
            caps = dev.capabilities()

            # Check for absolute positioning (digitizer characteristic)
            has_abs = ecodes.EV_ABS in caps
            if not has_abs:
                continue

            abs_caps = caps[ecodes.EV_ABS]
            abs_codes = {code for code, info in abs_caps}

            # Score device relevance
            score = 0
            keywords = ['ipts', 'pen', 'touch', 'stylus', 'surface', 'digitizer', 'pointer']
            for kw in keywords:
                if kw in name_lower:
                    score += 10

            # Pen/touch specific event codes
            if ecodes.ABS_X in abs_codes and ecodes.ABS_Y in abs_codes:
                score += 5
            if ecodes.ABS_PRESSURE in abs_codes:
                score += 5
            if ecodes.ABS_TILT_X in abs_codes or ecodes.ABS_TILT_Y in abs_codes:
                score += 5
            if ecodes.BTN_TOOL_PEN in caps.get(ecodes.EV_KEY, {}):
                score += 5
            if ecodes.BTN_TOUCH in caps.get(ecodes.EV_KEY, {}):
                score += 3

            if score >= 5:
                device_info = {
                    'path': path,
                    'name': dev.name,
                    'phys': dev.phys if hasattr(dev, 'phys') else '',
                    'score': score,
                    'capabilities': {
                        'has_pen': ecodes.BTN_TOOL_PEN in caps.get(ecodes.EV_KEY, {}),
                        'has_touch': ecodes.BTN_TOUCH in caps.get(ecodes.EV_KEY, {}),
                        'has_pressure': ecodes.ABS_PRESSURE in abs_codes,
                        'has_tilt': ecodes.ABS_TILT_X in abs_codes or ecodes.ABS_TILT_Y in abs_codes,
                    }
                }
                devices.append(device_info)
                logger.debug(f"Found device: {dev.name} (score={score}, path={path})")

        except (PermissionError, OSError, Exception) as e:
            logger.debug(f"Skipping {path}: {e}")
            continue

    # Sort by score (most relevant first)
    devices.sort(key=lambda d: d['score'], reverse=True)
    return devices


# ---------------------------------------------------------------------------
# Event Serializer
# ---------------------------------------------------------------------------
class EventSerializer:
    """
    Serializes evdev input events to MessagePack packets
    for network transmission.
    """

    def __init__(self, device_info: dict):
        self.device_info = device_info
        self.abs_ranges: dict[int, dict] = {}
        self._init_abs_ranges()

    def _init_abs_ranges(self):
        """Read absolute axis ranges from the device."""
        try:
            dev = InputDevice(self.device_info['path'])
            caps = dev.capabilities()
            if ecodes.EV_ABS in caps:
                for code, info in caps[ecodes.EV_ABS]:
                    self.abs_ranges[code] = {
                        'min': info.min,
                        'max': info.max,
                        'fuzz': info.fuzz if hasattr(info, 'fuzz') else 0,
                        'flat': info.flat if hasattr(info, 'flat') else 0,
                        'res': info.resolution if hasattr(info, 'resolution') else None,
                    }
        except Exception as e:
            logger.warning(f"Failed to read abs ranges: {e}")

    def normalize(self, code: int, value: int) -> float:
        """Normalize absolute axis value to [0, 1]."""
        rng = self.abs_ranges.get(code, {})
        min_val = rng.get('min', 0)
        max_val = rng.get('max', 1)
        if max_val <= min_val:
            return 0.0
        return max(0.0, min(1.0, (value - min_val) / (max_val - min_val)))

    def normalize_tilt(self, code: int, value: int) -> float:
        """Normalize tilt to [-1, 1]."""
        rng = self.abs_ranges.get(code, {})
        min_val = rng.get('min', -90)
        max_val = rng.get('max', 90)
        if max_val <= min_val:
            return 0.0
        return max(-1.0, min(1.0, (value - min_val) / (max_val - min_val) * 2 - 1))

    def serialize_event(self, event: Any, state: dict) -> Optional[dict]:
        """
        Process an evdev event and update state.
        Returns a packet dict if the event should be sent.
        """
        if event.type == ecodes.EV_ABS:
            if event.code == ecodes.ABS_X:
                state['x'] = self.normalize(event.code, event.value)
            elif event.code == ecodes.ABS_Y:
                state['y'] = self.normalize(event.code, event.value)
            elif event.code == ecodes.ABS_PRESSURE:
                state['pressure'] = self.normalize(event.code, event.value)
            elif event.code == ecodes.ABS_TILT_X:
                state['tilt_x'] = self.normalize_tilt(event.code, event.value)
            elif event.code == ecodes.ABS_TILT_Y:
                state['tilt_y'] = self.normalize_tilt(event.code, event.value)
            elif event.code == ecodes.ABS_DISTANCE:
                state['distance'] = self.normalize(event.code, event.value)
            elif event.code == ecodes.ABS_MT_POSITION_X:
                state['x'] = self.normalize(event.code, event.value)
            elif event.code == ecodes.ABS_MT_POSITION_Y:
                state['y'] = self.normalize(event.code, event.value)
            elif event.code == ecodes.ABS_MT_PRESSURE:
                state['pressure'] = self.normalize(event.code, event.value)
            elif event.code == ecodes.ABS_MT_TRACKING_ID:
                state['tracking_id'] = event.value if event.value >= 0 else -1

        elif event.type == ecodes.EV_KEY:
            if event.code == ecodes.BTN_TOUCH:
                state['touching'] = bool(event.value)
            elif event.code == ecodes.BTN_TOOL_PEN:
                state['in_range'] = bool(event.value)
                state['tool'] = 'pen' if event.value else 'none'
            elif event.code == ecodes.BTN_TOOL_RUBBER:
                state['tool'] = 'eraser' if event.value else state.get('tool', 'none')
            elif event.code == ecodes.BTN_STYLUS:
                state['button_1'] = bool(event.value)
            elif event.code == ecodes.BTN_STYLUS2:
                state['button_2'] = bool(event.value)

        elif event.type == ecodes.EV_SYN and event.code == ecodes.SYN_REPORT:
            # Send packet on SYN_REPORT
            return self._build_packet(state)

        return None

    def _build_packet(self, state: dict) -> dict:
        """Build network packet from current state."""
        return {
            't': time.time(),
            'seq': 0,  # Filled by sender
            'x': round(state.get('x', 0.0), 5),
            'y': round(state.get('y', 0.0), 5),
            'p': round(state.get('pressure', 0.0), 4),
            'tx': round(state.get('tilt_x', 0.0), 4),
            'ty': round(state.get('tilt_y', 0.0), 4),
            'tip': state.get('touching', False),
            'rng': state.get('in_range', False),
            'tool': state.get('tool', 'none'),
            'btn1': state.get('button_1', False),
            'btn2': state.get('button_2', False),
            'tid': state.get('tracking_id', -1),
        }


# ---------------------------------------------------------------------------
# Input Forwarder
# ---------------------------------------------------------------------------
class InputForwarder:
    """
    Captures input events and forwards to host via UDP.
    """

    def __init__(self, host: str, port: int, device_path: Optional[str] = None):
        self.host = host
        self.port = port
        self.device_path = device_path
        self._running = False
        self._socket: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._seq = 0

    def _get_device(self) -> str:
        """Find and return the best input device path."""
        if self.device_path:
            return self.device_path

        devices = find_surface_input_devices()
        if not devices:
            raise RuntimeError("No Surface input device found")

        logger.info("Found input devices:")
        for i, d in enumerate(devices[:5]):
            caps = d['capabilities']
            cap_str = ', '.join(k for k, v in caps.items() if v)
            logger.info(f"  [{i+1}] {d['name']} ({cap_str}) - {d['path']}")

        best = devices[0]
        logger.info(f"Selected: {best['name']} at {best['path']}")
        return best['path']

    def _send_loop(self, device_path: str):
        """Main capture and send loop."""
        try:
            device = InputDevice(device_path)
            device.grab()
            logger.info(f"Grabbed device: {device.name}")
        except PermissionError:
            logger.error(f"Permission denied on {device_path}. Run as root or add user to 'input' group.")
            return
        except Exception as e:
            logger.error(f"Failed to open device: {e}")
            return

        # Setup socket
        try:
            self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._socket.settimeout(1.0)
        except Exception as e:
            logger.error(f"Failed to create socket: {e}")
            return

        # Initialize serializer and state
        device_info = {'path': device_path, 'name': device.name}
        serializer = EventSerializer(device_info)
        state = {
            'x': 0.0, 'y': 0.0, 'pressure': 0.0,
            'tilt_x': 0.0, 'tilt_y': 0.0,
            'touching': False, 'in_range': False,
            'tool': 'none', 'button_1': False, 'button_2': False,
            'tracking_id': -1, 'distance': 1.0,
        }

        last_send = 0
        send_interval = 1.0 / SEND_RATE_HZ
        packets_sent = 0
        last_stats = time.time()

        logger.info(f"Forwarding input events to {self.host}:{self.port}")

        while self._running:
            try:
                # Read event (blocking with timeout via select)
                import select
                ready, _, _ = select.select([device.fd], [], [], 0.5)
                if not ready:
                    continue

                for event in device.read():
                    packet = serializer.serialize_event(event, state)
                    if packet:
                        packet['seq'] = self._seq
                        self._seq += 1

                        # Rate limit sending
                        now = time.time()
                        if now - last_send >= send_interval:
                            data = msgpack.packb(packet, use_bin_type=True)
                            self._socket.sendto(data, (self.host, self.port))
                            packets_sent += 1
                            last_send = now

                        # Stats
                        if now - last_stats >= 10.0:
                            logger.info(f"Input stats: {packets_sent} packets/10s ({packets_sent/10:.0f} pps)")
                            packets_sent = 0
                            last_stats = now

            except OSError as e:
                if self._running:
                    logger.error(f"Socket error: {e}")
                break
            except Exception as e:
                logger.error(f"Capture error: {e}")
                time.sleep(0.5)

        # Cleanup
        try:
            device.ungrab()
        except:
            pass
        logger.info("Input forwarder stopped")

    def start(self):
        """Start input forwarding."""
        self._running = True
        device_path = self._get_device()

        self._thread = threading.Thread(target=self._send_loop, args=(device_path,), daemon=True)
        self._thread.start()
        logger.info("Input forwarder started")

    def stop(self):
        """Stop input forwarding."""
        self._running = False
        if self._socket:
            try:
                self._socket.close()
            except:
                pass
        if self._thread:
            self._thread.join(timeout=3.0)
        logger.info("Input forwarder stopped")

    def is_running(self) -> bool:
        return self._running and self._thread and self._thread.is_alive()


# ---------------------------------------------------------------------------
# Interactive Device Selector
# ---------------------------------------------------------------------------
def interactive_device_select() -> Optional[str]:
    """Let user select an input device interactively."""
    devices = find_surface_input_devices()

    if not devices:
        print("No input devices with absolute positioning found.")
        print("Available devices:")
        for path in list_devices():
            try:
                dev = InputDevice(path)
                print(f"  {path}: {dev.name}")
            except:
                pass
        return None

    print("\nDetected input devices:")
    for i, d in enumerate(devices, 1):
        caps = d['capabilities']
        features = [k.replace('has_', '') for k, v in caps.items() if v]
        print(f"  [{i}] {d['name']}")
        print(f"      Path: {d['path']}")
        print(f"      Features: {', '.join(features) if features else 'basic'}")
        print()

    try:
        choice = input(f"Select device [1-{len(devices)}]: ").strip()
        idx = int(choice) - 1
        if 0 <= idx < len(devices):
            return devices[idx]['path']
    except (ValueError, KeyboardInterrupt, EOFError):
        pass

    return devices[0]['path'] if devices else None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description='SP7 Input Forwarder - Captures touch/pen and sends to host',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --host 192.168.1.100
  %(prog)s --host 192.168.1.100 --port 5005 --device /dev/input/event8
  %(prog)s --list-devices
        """
    )
    parser.add_argument('--host', default=DEFAULT_HOST, help='Host PC IP address')
    parser.add_argument('--port', '-p', type=int, default=DEFAULT_PORT, help='UDP port')
    parser.add_argument('--device', '-d', help='Specific input device path')
    parser.add_argument('--list-devices', '-l', action='store_true', help='List available devices and exit')
    parser.add_argument('--rate', '-r', type=int, default=SEND_RATE_HZ, help=f'Send rate in Hz (default: {SEND_RATE_HZ})')
    parser.add_argument('--verbose', '-v', action='store_true', help='Verbose logging')

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # List devices mode
    if args.list_devices:
        devices = find_surface_input_devices()
        if not devices:
            print("No Surface-like input devices found.")
            sys.exit(1)
        print(f"\nFound {len(devices)} input device(s):\n")
        for d in devices:
            print(f"  {d['path']}: {d['name']}")
            print(f"    Score: {d['score']}, Caps: {d['capabilities']}")
        sys.exit(0)

    # Auto-select device if not specified
    device_path = args.device
    if not device_path:
        device_path = interactive_device_select()
        if not device_path:
            print("No device selected.")
            sys.exit(1)

    global SEND_RATE_HZ
    SEND_RATE_HZ = args.rate

    # Create and start forwarder
    forwarder = InputForwarder(args.host, args.port, device_path)

    signal.signal(signal.SIGINT, lambda s, f: forwarder.stop())
    signal.signal(signal.SIGTERM, lambda s, f: forwarder.stop())

    forwarder.start()

    try:
        while forwarder.is_running():
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        forwarder.stop()


if __name__ == '__main__':
    main()
