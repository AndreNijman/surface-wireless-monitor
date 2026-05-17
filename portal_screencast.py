#!/usr/bin/env python3
import os
import sys
import uuid
import dbus
import dbus.mainloop.glib
from gi.repository import GLib

# We use dbus-python with GLib mainloop to handle signals
dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)

class PortalScreenCast:
    def __init__(self):
        self.bus = dbus.SessionBus()
        self.portal = self.bus.get_object('org.freedesktop.portal.Desktop',
                                          '/org/freedesktop/portal/desktop')
        self.screencast = dbus.Interface(self.portal, 'org.freedesktop.portal.ScreenCast')
        
        self.sender_name = self.bus.get_unique_name()[1:].replace('.', '_')
        self.loop = GLib.MainLoop()
        
        self.session_path = None
        self.pipewire_fd = None
        self.node_id = None
        
        # Start the sequence
        self.create_session()
        self.loop.run()
        
    def _create_handle_token(self):
        return f"token_{uuid.uuid4().hex}"

    def _get_request_path(self, token):
        return f"/org/freedesktop/portal/desktop/request/{self.sender_name}/{token}"

    def create_session(self):
        token = self._create_handle_token()
        req_path = self._get_request_path(token)
        
        def on_response(response, results):
            self.bus.remove_signal_receiver(on_response, signal_name='Response', path=req_path)
            if response != 0:
                print(f"CreateSession failed: {response}")
                self.loop.quit()
                return
            self.session_path = results['session_handle']
            self.select_sources()

        self.bus.add_signal_receiver(on_response, signal_name='Response',
                                     dbus_interface='org.freedesktop.portal.Request',
                                     path=req_path)
        
        def on_reply(req_path_ret):
            pass
            
        def on_error(e):
            print(f"CreateSession call failed: {e}")
            self.loop.quit()

        self.screencast.CreateSession({
            'session_handle_token': dbus.String(token, variant_level=1),
            'handle_token': dbus.String(token, variant_level=1)
        }, reply_handler=on_reply, error_handler=on_error)

    def select_sources(self):
        token = self._create_handle_token()
        req_path = self._get_request_path(token)
        
        def on_response(response, results):
            self.bus.remove_signal_receiver(on_response, signal_name='Response', path=req_path)
            if response != 0:
                print(f"SelectSources failed: {response}")
                self.loop.quit()
                return
            self.start_cast()

        self.bus.add_signal_receiver(on_response, signal_name='Response',
                                     dbus_interface='org.freedesktop.portal.Request',
                                     path=req_path)
        
        def on_reply(req_path_ret):
            pass
            
        def on_error(e):
            print(f"SelectSources call failed: {e}")
            self.loop.quit()

        # Options: 1 = Monitor, 2 = Window, 3 = Monitor | Window
        self.screencast.SelectSources(self.session_path, {
            'multiple': dbus.Boolean(False, variant_level=1),
            'types': dbus.UInt32(1, variant_level=1), # Monitor
            'handle_token': dbus.String(token, variant_level=1)
        }, reply_handler=on_reply, error_handler=on_error)

    def start_cast(self):
        token = self._create_handle_token()
        req_path = self._get_request_path(token)
        
        def on_response(response, results):
            self.bus.remove_signal_receiver(on_response, signal_name='Response', path=req_path)
            if response != 0:
                print(f"Start failed: {response}")
                self.loop.quit()
                return
            
            # Extract node_id. streams is a array of (uint32 node_id, dict properties)
            streams = results.get('streams', [])
            if streams:
                self.node_id = int(streams[0][0])
            else:
                print("No streams returned")
                self.loop.quit()
                return
                
            self.open_pipewire_remote()

        self.bus.add_signal_receiver(on_response, signal_name='Response',
                                     dbus_interface='org.freedesktop.portal.Request',
                                     path=req_path)
        
        def on_reply(req_path_ret):
            pass
            
        def on_error(e):
            print(f"Start call failed: {e}")
            self.loop.quit()

        self.screencast.Start(self.session_path, '', {
            'handle_token': dbus.String(token, variant_level=1)
        }, reply_handler=on_reply, error_handler=on_error)

    def open_pipewire_remote(self):
        def on_reply(fd_dbus):
            self.pipewire_fd = fd_dbus.take()
            self.loop.quit()
            
        def on_error(e):
            print(f"OpenPipeWireRemote call failed: {e}")
            self.loop.quit()
            
        self.screencast.OpenPipeWireRemote(self.session_path, {},
                                           reply_handler=on_reply, error_handler=on_error)

def open_screencast():
    """
    Initiates a ScreenCast session via xdg-desktop-portal.
    Returns (pipewire_fd, node_id).
    """
    psc = PortalScreenCast()
    if psc.pipewire_fd is None or psc.node_id is None:
        raise RuntimeError("Failed to open screencast")
    return psc.pipewire_fd, psc.node_id

if __name__ == '__main__':
    import gi
    gi.require_version('Gst', '1.0')
    from gi.repository import Gst

    print("Opening ScreenCast portal...")
    try:
        fd, node_id = open_screencast()
    except RuntimeError as e:
        print(e)
        sys.exit(1)
        
    print(f"Got PipeWire FD: {fd}, Node ID: {node_id}")
    
    Gst.init(None)
    
    # We construct a pipeline string using the fd and node_id
    pipeline_str = f"pipewiresrc fd={fd} path={node_id} ! videoconvert ! fakesink name=sink"
    print(f"Running pipeline: {pipeline_str}")
    
    pipeline = Gst.parse_launch(pipeline_str)
    
    sink = pipeline.get_by_name("sink")
    sink_pad = sink.get_static_pad("sink")
    
    frame_count = 0
    def on_buffer(pad, info):
        global frame_count
        frame_count += 1
        if frame_count == 1 or frame_count % 30 == 0:
            print(f"Got frame {frame_count}")
        return Gst.PadProbeReturn.OK
        
    sink_pad.add_probe(Gst.PadProbeType.BUFFER, on_buffer)
    
    loop = GLib.MainLoop()
    
    # Stop after 5 seconds
    def timeout():
        print("5 seconds elapsed. Stopping pipeline...")
        loop.quit()
        return False
        
    GLib.timeout_add_seconds(5, timeout)
    
    bus = pipeline.get_bus()
    def on_message(bus, message):
        t = message.type
        if t == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            print(f"Error: {err}, {debug}")
            loop.quit()
        elif t == Gst.MessageType.EOS:
            print("EOS reached")
            loop.quit()
        return True
        
    bus.add_watch(GLib.PRIORITY_DEFAULT, on_message)
    
    pipeline.set_state(Gst.State.PLAYING)
    loop.run()
    pipeline.set_state(Gst.State.NULL)
    print("Test completed.")
