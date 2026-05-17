# Host Dependencies - Void Linux

This guide covers installing the host streaming software on **Void Linux**.
The live ISO being built for the Surface Pro 7 is still Debian-based — these instructions are for the **host PC** that streams its display to the SP7.

## Install Host Streaming Dependencies

```bash
# 1. Install GStreamer and Python dependencies
sudo xbps-install -Sy \
    gstreamer1 \
    gst-plugins-base1 \
    gst-plugins-good1 \
    gst-plugins-bad1 \
    gst-libav \
    gst-vaapi \
    intel-media-driver \
    libva-intel-driver \
    python3 \
    python3-pip \
    python3-gobject \
    python3-evdev \
    avahi \
    dbus-elogind \
    dbus

# 2. Install Python packages (not in void repos)
pip3 install --user msgpack python-zeroconf websockets aiohttp

# 3. Optional: Intel hardware encoding support
sudo xbps-install -Sy \
    intel-video-accel \
    libva-utils
```

## Run the Host Streamer

```bash
# Replace 192.168.1.50 with your SP7's IP address
python3 receiver/sp7-host-stream.py --target 192.168.1.50 --verbose
```

## Firewall (if enabled)

```bash
# Open UDP ports for video and input
sudo iptables -I INPUT -p udp --dport 5004 -j ACCEPT
sudo iptables -I INPUT -p udp --dport 5005 -j ACCEPT

# Or with nftables:
sudo nft add rule inet filter input udp dport { 5004, 5005 } accept
```

## Quick Test Without SP7 (loopback)

```bash
# Stream to localhost to verify pipeline works
python3 receiver/sp7-host-stream.py --target 127.0.0.1 --verbose

# In another terminal, receive it:
gst-launch-1.0 udpsrc port=5004 caps="application/x-rtp,media=video,encoding-name=H264,payload=96" ! \
    rtpjitterbuffer latency=20 ! rtph264depay ! h264parse ! \
    vaapih264dec low-latency=1 ! videoconvert ! xvimagesink sync=false
```

## Troubleshooting

### "No encoder found" error
Install additional GStreamer encoders:
```bash
sudo xbps-install -Sy gst-plugins-ugly1 x264
```

### VAAPI not working on Intel
```bash
# Check VAAPI status
vainfo

# If it fails, try setting the driver:
export LIBVA_DRIVER_NAME=iHD   # For newer Intel (Skylake+)
# or
export LIBVA_DRIVER_NAME=i965  # For older Intel
```

### High latency
```bash
# Check CPU governor
cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor
# If not "performance", set it:
echo performance | sudo tee /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor
```

### Avahi (mDNS) not working
```bash
# Start avahi daemon
sudo ln -s /etc/sv/avahi-daemon /var/service/
sudo sv start avahi-daemon
```
