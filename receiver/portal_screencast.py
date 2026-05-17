#!/usr/bin/env python3
"""
portal_screencast.py — desktop capture for Wayland (Hyprland/wlroots) via
the org.freedesktop.portal.ScreenCast D-Bus interface.

Returns a PipeWire fd + node id for use with GStreamer:
    pipewiresrc fd=<fd> path=<node_id>

The portal flow is: CreateSession -> SelectSources -> Start -> (each a
Request whose Response signal carries the result) -> OpenPipeWireRemote
(returns the fd directly). Every portal call MUST carry a handle_token,
and CreateSession also a session_handle_token — omitting them is what
produces the "Missing token" error.

Routing: on Hyprland with only xdg-desktop-portal-wlr installed, ensure
~/.config/xdg-desktop-portal/portals.conf routes ScreenCast to 'wlr'
(see ensure_portal_config()).
"""

import os
import sys
import dbus
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib

BUS_NAME = "org.freedesktop.portal.Desktop"
OBJ_PATH = "/org/freedesktop/portal/desktop"
SC_IFACE = "org.freedesktop.portal.ScreenCast"
REQUEST_IFACE = "org.freedesktop.portal.Request"


def ensure_portal_config() -> None:
    """Make sure xdg-desktop-portal routes ScreenCast to the wlr backend
    and that wlr won't block on an interactive output chooser."""
    cfg_dir = os.path.expanduser("~/.config/xdg-desktop-portal")
    os.makedirs(cfg_dir, exist_ok=True)
    portals_conf = os.path.join(cfg_dir, "portals.conf")
    if not os.path.exists(portals_conf):
        with open(portals_conf, "w") as f:
            f.write("[preferred]\n"
                    "default=gtk\n"
                    "org.freedesktop.impl.portal.ScreenCast=wlr\n")
    wlr_dir = os.path.expanduser("~/.config/xdg-desktop-portal-wlr")
    os.makedirs(wlr_dir, exist_ok=True)
    wlr_conf = os.path.join(wlr_dir, "config")
    if not os.path.exists(wlr_conf):
        with open(wlr_conf, "w") as f:
            f.write("[screencast]\nmax_fps=30\nchooser_type=none\n")


def open_screencast(timeout: int = 25) -> tuple:
    """Open a portal ScreenCast session. Returns (pipewire_fd, node_id)."""
    DBusGMainLoop(set_as_default=True)
    bus = dbus.SessionBus()
    sc = dbus.Interface(bus.get_object(BUS_NAME, OBJ_PATH), SC_IFACE)

    # Request objects live at a path predictable from our bus name + token,
    # so we can subscribe to the Response signal *before* making the call.
    sender = bus.get_unique_name()[1:].replace('.', '_')
    loop = GLib.MainLoop()
    state = {'session': None, 'node': None, 'fd': None, 'error': None}
    seq = [0]

    def token(prefix: str) -> str:
        seq[0] += 1
        return f"sp7{prefix}{seq[0]}"

    def on_request(tok: str, handler):
        path = f"/org/freedesktop/portal/desktop/request/{sender}/{tok}"
        obj = bus.get_object(BUS_NAME, path)
        dbus.Interface(obj, REQUEST_IFACE).connect_to_signal("Response", handler)

    def fail(msg: str):
        state['error'] = msg
        loop.quit()

    def opts(**kw) -> dbus.Dictionary:
        return dbus.Dictionary(kw, signature='sv')

    # Step 1: CreateSession
    def do_create():
        ht, st = token('h'), token('s')
        on_request(ht, on_create)
        sc.CreateSession(opts(handle_token=dbus.String(ht),
                              session_handle_token=dbus.String(st)))

    def on_create(code, results):
        if code != 0:
            return fail(f"CreateSession denied (code {code})")
        state['session'] = results['session_handle']
        ht = token('h')
        on_request(ht, on_select)
        # types: 1=MONITOR  cursor_mode: 2=EMBEDDED (cursor drawn in video)
        sc.SelectSources(state['session'],
                         opts(handle_token=dbus.String(ht),
                              types=dbus.UInt32(1),
                              multiple=dbus.Boolean(False),
                              cursor_mode=dbus.UInt32(2)))

    def on_select(code, results):
        if code != 0:
            return fail(f"SelectSources denied (code {code})")
        ht = token('h')
        on_request(ht, on_start)
        sc.Start(state['session'], "", opts(handle_token=dbus.String(ht)))

    def on_start(code, results):
        if code != 0:
            return fail(f"Start denied (code {code})")
        streams = results.get('streams')
        if not streams:
            return fail("Start returned no streams")
        state['node'] = int(streams[0][0])
        try:
            # OpenPipeWireRemote returns the fd directly (not a Request).
            fd = sc.OpenPipeWireRemote(state['session'], opts())
            state['fd'] = fd.take() if hasattr(fd, 'take') else int(fd)
        except dbus.exceptions.DBusException as e:
            return fail(f"OpenPipeWireRemote failed: {e}")
        loop.quit()

    ensure_portal_config()
    do_create()
    GLib.timeout_add_seconds(timeout, lambda: fail("timed out waiting for portal"))
    loop.run()

    if state['error']:
        raise RuntimeError(state['error'])
    if state['fd'] is None or state['node'] is None:
        raise RuntimeError("portal did not return an fd/node")
    return state['fd'], state['node']


def gst_source(fd: int, node_id: int) -> str:
    """GStreamer source fragment for the screencast."""
    return f"pipewiresrc fd={fd} path={node_id} do-timestamp=true"


if __name__ == "__main__":
    import subprocess
    print("[*] Opening screencast via xdg-desktop-portal...")
    try:
        fd, node = open_screencast()
    except Exception as e:
        print(f"[FAILED] {e}", file=sys.stderr)
        sys.exit(1)
    print(f"[OK] fd={fd} node={node}")
    print(f"[*] Verifying with GStreamer: {gst_source(fd, node)}")
    try:
        rc = subprocess.run(
            ["gst-launch-1.0", "-e", *gst_source(fd, node).split(),
             "num-buffers=150", "!", "videoconvert", "!", "fakesink"],
            timeout=25).returncode
        print("[SUCCESS] captured 150 frames from the live screen" if rc == 0
              else f"[gst exited rc={rc}]")
    except subprocess.TimeoutExpired:
        print("[gst-launch ran 25s without finishing — capture is live "
              "but num-buffers EOS did not fire]")
