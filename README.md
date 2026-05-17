# SP7 Wireless Touchscreen Monitor - Bootable Live ISO

Turn your **Surface Pro 7** into a wireless touchscreen monitor with full **Surface Pen support** - without touching the internal storage. Boot from USB, and your Surface becomes a portable wireless display with pressure-sensitive pen input.

## What's Included

- **Bootable Live ISO** - Boots directly from USB, runs entirely in RAM
- **linux-surface kernel** - Full touch and pen support for Surface Pro 7
- **iptsd daemon** - Intel Precise Touch & Stylus driver for 4096-level pen pressure
- **Hardware-accelerated video** - Intel VAAPI H.264 decode for minimal latency
- **Auto-discovery** - Finds your host PC automatically via mDNS
- **Cross-platform host** - Works with both Linux and Windows host PCs

## Target Latency
- **20-40ms** end-to-end (display + input round-trip)
- **15-28ms** display path (capture→encode→stream→decode→display)
- **6-10ms** input path (digitizer→network→injection)

## Quick Start

### Step 1: Build the ISO (on any Debian/Ubuntu machine)

```bash
# Clone or extract this directory, then:
cd sp7-wireless-monitor-iso
sudo ./build-iso.sh

# Output: sp7-wireless-monitor.iso (in the same directory)
```

**Prerequisites:** Any Linux with `sudo`, 4GB RAM, 10GB free disk space, internet connection.

The build script auto-detects your package manager (`xbps` for Void, `apt` for Debian/Ubuntu, `dnf` for Fedora, `pacman` for Arch) and installs build tools accordingly. The live system being built is always Debian-based.

### Step 2: Flash to USB

**With Etcher (GUI - recommended):**
1. Download [balenaEtcher](https://www.balena.io/etcher/)
2. Select `sp7-wireless-monitor.iso`
3. Select your USB drive (8GB+ recommended)
4. Click Flash

**With `dd` (command line):**
```bash
# Replace /dev/sdX with your USB device (find with: lsblk)
sudo dd if=sp7-wireless-monitor.iso of=/dev/sdX bs=4M status=progress
sudo sync
```

**With Rufus (Windows):**
1. Download [Rufus](https://rufus.ie/)
2. Select USB device and the ISO file
3. Partition scheme: GPT, Target system: UEFI (non-CSM)
4. Click Start

### Step 3: Boot Surface Pro 7 from USB

**Method 1 - Boot Menu (temporary):**
1. Shut down Surface completely
2. Hold **Volume Down** button
3. Press and release **Power** button
4. Keep holding Volume Down until the GRUB menu appears
5. Select "SP7 Wireless Monitor (Live)" and press Enter

**Method 2 - UEFI Settings (to change boot order):**
1. Shut down Surface
2. Hold **Volume Up** button
3. Press and release **Power** button
4. Keep holding Volume Up until UEFI settings appear
5. Go to **Boot configuration**
6. Move **USB Storage** to the top of the boot order
7. Exit and save changes

**Disable Secure Boot (one-time setup):**
1. Enter UEFI settings (Method 2 above)
2. Go to **Security** > **Secure Boot**
3. Set **Secure Boot** to **Disabled**
4. Exit and save

> The linux-surface kernel is not signed by Microsoft, so Secure Boot must be disabled. Your existing Windows installation is completely unaffected.

### Step 4: Run Host Software

**On Void Linux host:**
```bash
# Install dependencies (xbps)
sudo xbps-install -Sy gstreamer1 gst-plugins-base1 gst-plugins-good1 \
    gst-plugins-bad1 gst-libav gst-vaapi python3 python3-pip \
    python3-gobject python3-evdev avahi
pip3 install --user msgpack python-zeroconf

# Run the host streamer
python3 receiver/sp7-host-stream.py --target <SP7_IP_ADDRESS>
```
See [HOST-DEPS-Void.md](HOST-DEPS-Void.md) for full details including firewall, VAAPI, and troubleshooting.

**On Debian/Ubuntu host:**
```bash
# Install dependencies
sudo apt-get install gstreamer1.0-tools gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good gstreamer1.0-plugins-bad \
    gstreamer1.0-vaapi python3-gst-1.0 python3-evdev python3-msgpack

# Run the host streamer
python3 receiver/sp7-host-stream.py --target <SP7_IP_ADDRESS>
```

**On Windows host:**
See the [Windows host setup guide](WINDOWS_HOST.md) in the full research report.

### Step 5: Connect

The SP7 will automatically discover your host PC on the same WiFi network. If not found, you can enter the IP address manually on the SP7.

## Files in This Package

```
sp7-wireless-monitor-iso/
├── README.md                    # This file
├── build-iso.sh                 # ISO builder script (run with sudo)
├── receiver/                    # Receiver applications
│   ├── sp7-receiver.py          # Main receiver (video + input)
│   ├── sp7-input-client.py      # Standalone input forwarder
│   ├── sp7-host-stream.py       # Host streaming server
│   └── sp7-monitor              # Control script (start/stop/status)
├── overlay/                     # Files overlaid onto the live system
│   ├── etc/
│   │   ├── systemd/system/      # Systemd services
│   │   ├── sp7-monitor/         # Configuration
│   │   └── modprobe.d/          # Kernel module settings
│   └── usr/local/bin/           # Receiver binaries
└── boot/grub/                   # GRUB boot configuration
```

## System Architecture

```
Host PC (Linux/Windows)                    Surface Pro 7 (USB Boot)
=========================                  =========================
Display Capture                            Video Reception
  (DXGI/kmsgrab)                             (GStreamer + VAAPI)
       |                                             |
  H.264 Encode                                   Decode
  (QuickSync/VAAPI)                          (vaapih264dec)
       |                                             |
   UDP Stream     ---- Wi-Fi 6 (802.11ax) ---->   Display
  (RTP/DTLS)                                    (DRM/KMS)
       ^                                             |
       |                                    Touch/Pen Capture
   Input Events                             (iptsd → evdev)
  (UDP/MessagePack)                                |
       |                                       Forward
       +----<----<----<----<----<----<----<---- Send
```

## Hardware Requirements

### Surface Pro 7 (Receiver)
- Surface Pro 7 (any model: i3/i5/i7)
- 8GB+ USB flash drive
- Wi-Fi connection (5GHz recommended)
- Surface Pen (optional, for pen input)

### Host PC
- **Linux**: Debian 12+, Ubuntu 22.04+, or Arch Linux
- **Windows**: Windows 10/11 (see report for setup)
- Intel/AMD/NVIDIA GPU with H.264 hardware encoding
- Wi-Fi 5 (802.11ac) or Wi-Fi 6 (802.11ax) recommended

## Network Requirements
- Host and SP7 on the same local network
- 5GHz Wi-Fi band recommended for lowest latency
- 100+ Mbps sustained throughput between devices

## Customization

### Edit configuration before building
```bash
# Edit the default configuration
vim overlay/etc/sp7-monitor/config.conf

# Then rebuild
sudo ./build-iso.sh
```

### Change GStreamer pipeline
Edit `receiver/sp7-receiver.py` and modify `GST_PIPELINE` to adjust quality, latency, or codec settings.

### Add WiFi credentials for auto-connect
```bash
# Edit the setup script to add your WiFi
vim overlay/etc/systemd/system/sp7-monitor.service

# Add a line to ExecStartPre:
# ExecStartPre=/sbin/wpa_supplicant -B -i wlan0 -c /etc/wpa_supplicant.conf
```

## Troubleshooting

### ISO won't boot
- Ensure Secure Boot is **disabled** in UEFI settings
- Try the "Failsafe" boot option in GRUB
- Verify USB was flashed correctly (re-flash with Etcher)

### No video on SP7
- Check that WiFi is connected on both host and SP7
- Check host firewall: `sudo ufw allow 5004/udp` and `sudo ufw allow 5005/udp`
- Try the debug boot option: "SP7 Wireless Monitor (Debug)"
- Check logs on SP7: `journalctl -u sp7-monitor -f`

### Touch/pen not working
- Verify iptsd is running: `systemctl status iptsd`
- Check input devices: `python3 -c "import evdev; print([evdev.InputDevice(p).name for p in evdev.list_devices()])"`
- Ensure the linux-surface kernel is loaded: `uname -r` should contain "surface"

### High latency
- Use 5GHz Wi-Fi band (not 2.4GHz)
- Ensure WiFi power saving is disabled (handled automatically)
- Close bandwidth-heavy applications on the network
- Try lowering bitrate: edit config.conf on the SP7

## Technical Details

See the full research report for comprehensive technical documentation:
- Hardware analysis of Surface Pro 7
- Protocol design and latency budget
- Complete system architecture
- Implementation roadmap
- Risk assessment

## License

This project is provided as open-source research and implementation code for personal use.

## Credits

- [linux-surface](https://github.com/linux-surface/linux-surface) project for Surface kernel patches
- [iptsd](https://github.com/linux-surface/iptsd) for Intel Precise Touch & Stylus driver
- GStreamer team for the multimedia framework
- Intel VAAPI team for hardware acceleration
