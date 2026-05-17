# Surface Wireless Monitor

Turn a **Microsoft Surface Pro 7** into a **wireless touchscreen monitor** —
with full **Surface Pen** pressure and tilt — by booting it from a USB stick.
Nothing is installed to the Surface's internal SSD; Windows is left completely
untouched. The Surface boots a small Linux live system entirely into RAM,
receives your host PC's screen over Wi-Fi, displays it, and sends pen/touch
input back.

This repository contains the **ISO builder**, the **receiver software**, and
the **host streaming software** — everything needed to produce the bootable
USB and drive it from a host PC.

---

## Table of contents

- [How it works](#how-it-works)
- [Repository layout](#repository-layout)
- [Requirements](#requirements)
- [Part 1 — Build the ISO](#part-1--build-the-iso)
- [Part 2 — Flash the USB](#part-2--flash-the-usb)
- [Part 3 — Boot the Surface Pro 7](#part-3--boot-the-surface-pro-7)
- [Part 4 — Set up the host PC](#part-4--set-up-the-host-pc)
- [Part 5 — Connect](#part-5--connect)
- [Configuration](#configuration)
- [Software components](#software-components)
- [How the boot chain works](#how-the-boot-chain-works)
- [Troubleshooting](#troubleshooting)
- [Version history](#version-history)
- [Development workflow](#development-workflow)
- [Credits & license](#credits--license)

---

## How it works

```
   HOST PC  (Linux / Windows)                  SURFACE PRO 7  (USB live boot)
 ┌───────────────────────────┐               ┌───────────────────────────────┐
 │  Screen capture           │               │  GStreamer + VA-API           │
 │  (PipeWire / X11 / KMS)   │               │  H.264 hardware decode        │
 │            │              │               │            │                 │
 │  H.264 encode             │               │  Render via DRM/KMS           │
 │  (VA-API / NVENC / x264)  │               │  to the built-in display      │
 │            │              │   Wi-Fi 5/6   │            │                 │
 │       RTP / UDP  ─────────┼──────────────▶│       :5004  video in         │
 │       :5004               │               │                               │
 │                           │               │  iptsd → evdev                │
 │  uinput injection ◀───────┼───────────────┤  pen / touch capture          │
 │       :5005  input in     │   UDP/msgpack │       :5005  input out        │
 └───────────────────────────┘               └───────────────────────────────┘
```

1. The **host PC** captures its display, encodes it to H.264 (hardware
   accelerated where possible), and streams it as RTP over UDP to the Surface.
2. The **Surface Pro 7** boots the live USB, hardware-decodes the stream with
   Intel VA-API, and renders it full-screen.
3. The Surface's digitizer (pen + touch) is read through the **linux-surface**
   kernel and the **iptsd** daemon, normalised, and sent back to the host over
   UDP, where it is injected into the host's input system via `uinput`.
4. The host and Surface find each other automatically over **mDNS**
   (`_sp7monitor._tcp`); a manual IP entry is also available.

Target end-to-end latency is roughly **20–40 ms** on a clean 5 GHz network.

---

## Repository layout

```
surface-wireless-monitor/
├── README.md                       This file
├── build-iso.sh                    The ISO builder — run with sudo
├── receiver/                       Application code
│   ├── sp7-receiver.py             Main receiver (video display + input capture)
│   ├── sp7-input-client.py         Standalone input forwarder
│   ├── sp7-host-stream.py          Host-side: screen capture + encode + stream
│   ├── sp7-monitor                 Control script (start/stop/status/setup)
│   └── sp7-monitor-setup           First-boot configuration helper
├── overlay/                        Files copied verbatim into the live system
│   └── etc/
│       ├── systemd/system/         sp7-monitor.service, sp7-monitor-setup.service
│       ├── sp7-monitor/config.conf Default runtime configuration
│       └── modprobe.d/             Kernel module tuning (Wi-Fi power save off, …)
├── docs/
│   └── HOST-DEPS-Void.md           Host dependency guide for Void Linux
├── overlay-local/                  Local secrets — Wi-Fi credentials + SSH keys (git-ignored)
├── legacy/
│   └── zips/                       Archived zip iterations v1–v6 (see Version history)
└── build/                          Build scratch + ISO output (git-ignored)
```

`build/` and any `*.iso` / `*.img` / `*.log` are produced by the builder and
are intentionally **not** tracked by git.

---

## Requirements

### To build the ISO (the build host)

- A Linux machine with `sudo`/root. The builder auto-detects the package
  manager: **xbps** (Void), **apt** (Debian/Ubuntu), **dnf** (Fedora/RHEL),
  **pacman** (Arch). The live system it produces is always Debian-based — the
  build host's distro is only used to run the build tools.
- ~10 GB free disk space and a working internet connection (it downloads a
  Debian base system and the linux-surface kernel).
- A build takes roughly **30–60 minutes** depending on network speed.

### The Surface Pro 7 (the receiver)

- Any Surface Pro 7 (i3 / i5 / i7).
- A USB flash drive, **8 GB or larger**.
- Wi-Fi — 5 GHz strongly recommended for latency.
- A Surface Pen (optional, only for pen input).
- **Secure Boot must be disabled** (the linux-surface kernel is unsigned —
  see [Part 3](#part-3--boot-the-surface-pro-7)).

### The host PC (the source of the screen)

- **Linux** (Void, Debian 12+, Ubuntu 22.04+, Fedora, Arch) or **Windows 10/11**.
- A GPU with H.264 hardware encoding (Intel Quick Sync / VA-API, NVIDIA NVENC)
  — software `x264` encoding is used as a fallback.
- Same local network as the Surface; 5 GHz Wi-Fi or wired recommended.

---

## Part 1 — Build the ISO

From the repository root:

```bash
sudo ./build-iso.sh
```

The script runs ten steps and prints `[INFO]` / `[OK]` / `[ERROR]` lines:

| Step | What it does |
|------|--------------|
| 1  | Install build tools for the detected package manager (debootstrap, xorriso, squashfs-tools, grub, mtools, dosfstools, …) |
| 2  | `debootstrap` a minimal Debian *bookworm* base system into `build/chroot` |
| 3  | Bind-mount `/dev`, `/proc`, `/sys`, `/run` into the chroot |
| 4  | Configure locale, hostname, apt sources; add the **linux-surface** repo and install its kernel + `iptsd` |
| 5  | Install Python dependencies for the receiver |
| 6  | Copy the receiver apps and the `overlay/` tree into the system; enable the systemd service |
| 7  | Rebuild the initramfs, set kernel modules, create the `monitor` user |
| 8  | Build the compressed `filesystem.squashfs` root image |
| 9  | Build the GRUB EFI bootloader (with embedded early config), the BIOS GRUB image, and the FAT EFI System Partition image |
| 10 | Assemble the final hybrid ISO with `xorriso` |

Output: **`sp7-wireless-monitor.iso`** in the repository root.

> The build runs unattended. If it stops with `[ERROR]`, the message names the
> failing step. The `build/chroot` directory is safe to leave in place — Step 2
> always wipes it at the start of the next run.

---

## Part 2 — Flash the USB

The ISO is a hybrid image — write it raw to the whole USB device (not a
partition).

**balenaEtcher (GUI, easiest):**
1. Open [balenaEtcher](https://etcher.balena.io/).
2. Select `sp7-wireless-monitor.iso`, select the USB drive, click **Flash**.

**`dd` (Linux command line):**
```bash
lsblk                       # identify the USB device, e.g. /dev/sda
sudo umount /dev/sdX*       # unmount any mounted partitions first
sudo dd if=sp7-wireless-monitor.iso of=/dev/sdX bs=4M status=progress conv=fsync
sync
```
Replace `/dev/sdX` with the **whole device** (`/dev/sda`, not `/dev/sda1`).
Double-check with `lsblk` — this erases the target completely.

**Rufus (Windows):** select the ISO, partition scheme **GPT**, target
**UEFI (non-CSM)**, then Start.

---

## Part 3 — Boot the Surface Pro 7

### One-time: disable Secure Boot

The linux-surface kernel is not signed by Microsoft, so Secure Boot must be off.

1. Fully shut the Surface down.
2. Hold **Volume Up**, press and release **Power**, keep holding Volume Up
   until the UEFI screen appears.
3. **Security → Secure Boot → Disabled**.
4. Exit and save.

Windows on the internal SSD is unaffected by this change.

### Boot from the USB

1. Plug in the flashed USB stick.
2. Fully shut the Surface down.
3. Hold **Volume Down**, press and release **Power**, keep holding Volume Down
   until the GRUB menu appears.
4. Choose a menu entry:
   - **SP7 Wireless Monitor (Live)** — normal boot.
   - **Debug — verbose** — verbose logs, for diagnosing boot problems.
   - **Failsafe** — `nomodeset`, for graphics issues.

To make USB the permanent boot priority, enter UEFI (Volume Up method) →
**Boot configuration** → move **USB Storage** to the top.

---

## Part 4 — Set up the host PC

The host runs `receiver/sp7-host-stream.py`, which captures the screen and
streams it.

### Void Linux host

```bash
sudo xbps-install -Sy gstreamer1 gst-plugins-base1 gst-plugins-good1 \
    gst-plugins-bad1 gst-libav gst-vaapi intel-media-driver \
    python3 python3-pip python3-gobject python3-evdev avahi
pip3 install --user msgpack python-zeroconf
```
See [`docs/HOST-DEPS-Void.md`](docs/HOST-DEPS-Void.md) for the full Void guide,
including firewall rules, VA-API checks, and a loopback self-test.

### Debian / Ubuntu host

```bash
sudo apt-get install gstreamer1.0-tools gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good gstreamer1.0-plugins-bad gstreamer1.0-vaapi \
    gstreamer1.0-libav python3-gst-1.0 python3-evdev python3-msgpack \
    python3-zeroconf avahi-daemon
```

### Windows host

Windows host support uses DXGI desktop capture and Quick Sync encoding. This
is not yet packaged in this repository — track it as future work.

### Run the streamer

```bash
python3 receiver/sp7-host-stream.py --target <SURFACE_IP> --verbose
```

---

## Part 5 — Connect

1. Boot the Surface from USB and connect it to Wi-Fi.
2. Start `sp7-host-stream.py` on the host.
3. The Surface auto-discovers the host via mDNS. If discovery fails, the
   receiver prompts for the host IP — enter it manually.
4. The host screen appears on the Surface; pen and touch drive the host.

Ports used (open these if a firewall is active on the host):

| Port | Protocol | Purpose |
|------|----------|---------|
| 5004 | UDP | H.264 video, RTP — host → Surface |
| 5005 | UDP | Pen/touch input, msgpack — Surface → host |
| 5353 | UDP | mDNS service discovery |

---

## Configuration

Default runtime settings live in `overlay/etc/sp7-monitor/config.conf` and are
baked into the ISO at build time. Edit that file **before** building, or edit
`/etc/sp7-monitor/config.conf` on a running Surface and restart the service.

Common adjustments:

- **Bitrate / quality** — lower it on a congested network.
- **GStreamer pipeline** — edit `GST_PIPELINE` in `receiver/sp7-receiver.py`
  (the receiver tries VA-API hardware decode first, then a software fallback,
  then an X11 pipeline for development).
- **Resolution** — the receiver defaults to the SP7 native `2736×1824`;
  override with `--width` / `--height`.

The receiver CLI:

```
sp7-receiver.py [--host IP] [--video-port 5004] [--input-port 5005]
                [--no-input] [--width W] [--height H] [--verbose]
```

### Pre-loaded Wi-Fi and SSH access

The build can bake in credentials so the Surface comes up on the network and is
reachable over SSH with no manual setup:

- **Wi-Fi** — NetworkManager auto-connects to the configured networks on boot.
- **SSH** — `openssh-server` runs on boot. From a machine holding the matching
  private key:
  ```bash
  ssh root@sp7-monitor.local        # mDNS hostname (or use the IP)
  ```

These secrets live in **`overlay-local/`** — a git-ignored directory the build
script overlays onto the live system at build time (Step 6). They are kept out
of version control because this repository is public. Recreate it on any build
machine with this layout:

```
overlay-local/
├── etc/NetworkManager/system-connections/<name>.nmconnection   (chmod 600)
└── root/.ssh/authorized_keys                                   (chmod 600)
```

`.nmconnection` files are NetworkManager keyfiles; remove any `interface-name=`
line so they bind to whatever Wi-Fi device the Surface has. If `overlay-local/`
is absent the build still succeeds — it just produces an ISO with no pre-loaded
Wi-Fi or SSH key.

---

## Software components

| File | Runs on | Role |
|------|---------|------|
| `receiver/sp7-receiver.py` | Surface | Main app. Discovers the host (mDNS), receives the RTP/H.264 stream, hardware-decodes via VA-API, renders through DRM/KMS, and forwards digitizer events. |
| `receiver/sp7-input-client.py` | Surface | Standalone pen/touch forwarder — used when only input forwarding is wanted. |
| `receiver/sp7-host-stream.py` | Host PC | Captures the desktop (PipeWire/X11/KMS), encodes H.264 (VA-API/NVENC/x264 auto-detected), streams RTP, and injects received input via `uinput`. |
| `receiver/sp7-monitor` | Surface | Control wrapper: `start` / `stop` / `status` / `setup`. |
| `receiver/sp7-monitor-setup` | Surface | First-boot configuration helper. |
| `overlay/etc/systemd/system/sp7-monitor.service` | Surface | Auto-starts the receiver on boot. |
| `overlay/etc/systemd/system/sp7-monitor-setup.service` | Surface | Runs one-time setup on first boot. |
| `overlay/etc/modprobe.d/sp7-wireless-monitor.conf` | Surface | Disables Wi-Fi power saving and tunes modules for low latency. |

Input is normalised before transit: position to `[0,1]`, pressure to `[0,1]`,
tilt to `[-1,1]`, capped at a 120 Hz send rate, packed with msgpack.

---

## How the boot chain works

Getting a hand-built live ISO to boot a Surface Pro 7 from USB under UEFI is the
hard part. The chain:

1. **UEFI firmware** reads the GPT on the USB stick and finds the **EFI System
   Partition** — a real FAT filesystem image (`efiboot.img`, 33 MB FAT32)
   embedded as a GPT partition by `xorriso -isohybrid-gpt-basdat`.
2. Firmware loads **`/EFI/BOOT/BOOTX64.EFI`** from that FAT partition — a GRUB
   image built by `grub-mkimage` with all required modules and an **embedded
   early config**.
3. The embedded `early.cfg` runs:
   ```
   search --no-floppy --set=root --label "SP7MONITOR"
   set prefix=($root)/boot/grub
   configfile ($root)/boot/grub/grub.cfg
   ```
   This relocates `$root` from the small ESP onto the main **ISO9660** volume
   (labelled `SP7MONITOR`), where the real menu lives.
4. **`/boot/grub/grub.cfg`** presents the menu and loads the linux-surface
   kernel with `boot=live components`.
5. The **`live-boot`** initramfs hooks find and mount
   `/live/filesystem.squashfs`, and the system comes up entirely in RAM.

A legacy BIOS path (`grub.img`, `i386-pc-eltorito`) is also built as a
fallback, but the Surface Pro 7 is UEFI-only and never uses it.

Why a 33 MB FAT32 ESP: FAT32 is only spec-compliant at ≥ 65525 clusters. A
16 MB image falls below that, and strict UEFI FAT drivers (the Surface uses
one) can refuse to mount it.

---

## Troubleshooting

**ISO won't boot / drops to a `grub rescue>` prompt**
- Confirm Secure Boot is **disabled**.
- Re-flash the USB (a partial `dd` is a common cause).
- A rescue prompt usually means GRUB could not find the ISO volume — verify the
  build completed Step 10 and the ISO label is `SP7MONITOR`.

**No video on the Surface**
- Both devices on the same Wi-Fi? Prefer 5 GHz.
- Host firewall: allow UDP `5004` and `5005`.
- Boot the **Debug — verbose** entry and check: `journalctl -u sp7-monitor -f`.

**Pen / touch not working**
- `systemctl status iptsd` — the daemon must be running.
- `uname -r` should contain `surface` (the linux-surface kernel).
- List input devices:
  `python3 -c "import evdev; print([evdev.InputDevice(p).name for p in evdev.list_devices()])"`

**High latency**
- Use 5 GHz, not 2.4 GHz Wi-Fi.
- Lower the bitrate in `config.conf`.
- On the host, check the CPU governor is `performance` (see the Void host doc).

**Build fails**
- The `[ERROR]` line names the failing step.
- Step 1/2 failures are almost always network or package-name issues — see the
  [version history](#version-history) for the classes of bug already fixed.

---

## Version history

The build script went through several iterations before it could produce a
bootable image. The original zips are archived in `legacy/zips/`; from v7
onward each change is a git commit.

| Version | Change |
|---------|--------|
| **v1** | Initial build script. Debian/Ubuntu build host only. |
| **v2** | Added `live-boot`/`live-config` to the live system (without them the initramfs cannot mount the squashfs); added the BIOS `grub.img` step. |
| **v3** | Build host package-manager auto-detection (**xbps**/apt/dnf/pacman) so it runs on Void Linux; fixed the BIOS GRUB module list; added the Void host guide. |
| **v4** | Added the FAT **EFI System Partition** image (`efiboot.img`) — without it a `dd`-to-USB image has no mountable ESP and UEFI won't boot it. |
| **v5** | Embedded an early config in the GRUB EFI image so GRUB can locate `grub.cfg` on the ISO9660 volume; enlarged the ESP from 16 MB to a spec-compliant 33 MB FAT32. |
| **v6** | ISO volume label `SP7-MONITOR` → `SP7MONITOR` — a hyphen is not a valid ISO9660 identifier character and broke the GRUB label search. |
| **v7** | `debootstrap`: added `--components=main,contrib,non-free,non-free-firmware` so firmware/VA-API packages resolve; removed the non-existent `python3-asyncio` package. |
| **v8** | Removed the VA-API driver variants `i965-va-driver-shaders` and `intel-media-va-driver-non-free` from the debootstrap include list — they `Conflict` with the standard drivers pulled in by `va-driver-all`, and debootstrap's raw `dpkg` cannot resolve that the way `apt` can. |
| **v9** | Pre-load Wi-Fi and SSH: added `network-manager` and `openssh-server`, dropped `dhcpcd5`; the build now overlays a git-ignored `overlay-local/` tree carrying NetworkManager connections and `authorized_keys`, and enables the `NetworkManager` and `ssh` services. |
| **v10** | Fixed the linux-surface signing-key fetch: the old `surface-archive-keyring.gpg` path now 404s. Switched to the current `pkg/keys/surface.asc` (ASCII-armored) and pointed apt's `signed-by=` straight at the `.asc` (supported in bookworm — no `gpg --dearmor` step needed). |

---

## Development workflow

This repository is the source of truth; each change to the build script or
software is a git commit, using [Conventional Commits](https://www.conventionalcommits.org/)
(`feat:`, `fix:`, `chore:`, `docs:`, `refactor:`).

```bash
# make a change, then:
git add -p
git commit -m "fix(build): <what and why>"

# tag notable script versions:
git tag -a v8 -m "v8 — <summary>"
```

A new version = a new commit (and a tag for milestones). The `legacy/zips/`
archive is frozen — it only preserves the pre-git iterations.

---

## Credits & license

- [linux-surface](https://github.com/linux-surface/linux-surface) — Surface
  kernel patches and the `iptsd` digitizer daemon.
- [GStreamer](https://gstreamer.freedesktop.org/) — the multimedia pipeline.
- Intel VA-API — hardware video acceleration.
- [debootstrap](https://wiki.debian.org/Debootstrap),
  [live-boot](https://wiki.debian.org/DebianLive), GRUB, and `xorriso` — the
  live-ISO toolchain.

Provided as-is, for personal use. See individual upstream projects for their
respective licenses.
