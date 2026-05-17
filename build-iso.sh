#!/bin/bash
#
# SP7 Wireless Touchscreen Monitor - Live ISO Builder
# Run this on any Debian/Ubuntu machine to produce a bootable USB ISO
# Usage: sudo ./build-iso.sh
#

set -euo pipefail

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
NC='\033[0m'

info()  { echo -e "${BLUE}[INFO]${NC} $1"; }
ok()    { echo -e "${GREEN}[OK]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

# =============================================================================
# CONFIGURATION
# =============================================================================
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BUILD_DIR="${SCRIPT_DIR}/build"
ISO_DIR="${SCRIPT_DIR}/build/iso"
CHROOT_DIR="${SCRIPT_DIR}/build/chroot"
OVERLAY_DIR="${SCRIPT_DIR}/overlay"
RECEIVER_DIR="${SCRIPT_DIR}/receiver"

ISO_NAME="sp7-wireless-monitor.iso"
ISO_LABEL="SP7MONITOR"
ISO_OUTPUT="${SCRIPT_DIR}/${ISO_NAME}"

# Base system
DEBIAN_VERSION="bookworm"
ARCH="amd64"
MIRROR="http://deb.debian.org/debian"
KERNEL_FLAVOR="linux-image-surface"

# Size estimates
ROOTFS_SIZE_MB=2048

info "========================================"
info "  SP7 Wireless Monitor ISO Builder"
info "========================================"
info ""

# =============================================================================
# STEP 0: Check root
# =============================================================================
if [ "$EUID" -ne 0 ]; then
    error "This script must be run as root (required for debootstrap/chroot).\n   sudo ./build-iso.sh"
fi

# =============================================================================
# STEP 1: Install build dependencies (auto-detect package manager)
# =============================================================================
info "Step 1/10: Installing build dependencies..."

if command -v xbps-install &> /dev/null; then
    # Void Linux
    info "Detected package manager: xbps (Void Linux)"
    xbps-install -Sy \
        debootstrap squashfs-tools xorriso \
        grub-i386-efi grub-x86_64-efi \
        mtools dosfstools binutils \
        rsync curl wget \
        python3 python3-pip \
        2>&1 | tail -5

elif command -v apt-get &> /dev/null; then
    # Debian/Ubuntu
    info "Detected package manager: apt (Debian/Ubuntu)"
    apt-get update -qq
    apt-get install -y -qq \
        debootstrap squashfs-tools xorriso \
        grub-pc-bin grub-efi-amd64-bin grub-common \
        mtools dosfstools binutils \
        rsync curl wget \
        python3 python3-pip \
        2>&1 | tail -5

elif command -v dnf &> /dev/null; then
    # Fedora/RHEL
    info "Detected package manager: dnf (Fedora/RHEL)"
    dnf install -y \
        debootstrap squashfs-tools xorriso grub2-tools \
        mtools dosfstools binutils \
        rsync curl wget \
        python3 python3-pip \
        2>&1 | tail -5

elif command -v pacman &> /dev/null; then
    # Arch Linux
    info "Detected package manager: pacman (Arch)"
    pacman -Sy --noconfirm \
        debootstrap squashfs-tools xorriso grub \
        mtools dosfstools binutils \
        rsync curl wget \
        python3 python3-pip \
        2>&1 | tail -5

else
    error "No supported package manager found (tried: xbps, apt, dnf, pacman)"
    error "Please manually install: debootstrap squashfs-tools xorriso grub mtools dosfstools"
    exit 1
fi

ok "Dependencies installed"
info "NOTE: The live system being built is Debian-based (with systemd)."
info "      Your host OS ($(cat /etc/os-release 2>/dev/null | grep ^NAME | cut -d= -f2 | tr -d '"')) is only used for the build process."

# =============================================================================
# STEP 2: Create base filesystem with debootstrap
# =============================================================================
info "Step 2/10: Creating base Debian system..."

rm -rf "${CHROOT_DIR}"
mkdir -p "${CHROOT_DIR}"

debootstrap \
    --arch="${ARCH}" \
    --variant=minbase \
    --include=linux-image-amd64,systemd,systemd-sysv,udev,dbus,kmod,pciutils,usbutils,wireless-tools,wpasupplicant,iw,firmware-iwlwifi,firmware-linux-nonfree,firmware-misc-nonfree,ca-certificates,curl,wget,iputils-ping,iproute2,net-tools,dhcpcd5,openssh-client,avahi-daemon,avahi-utils,libnss-mdns,gstreamer1.0-tools,gstreamer1.0-plugins-base,gstreamer1.0-plugins-good,gstreamer1.0-plugins-bad,gstreamer1.0-vaapi,gstreamer1.0-libav,libgstreamer1.0-0,va-driver-all,i965-va-driver-shaders,intel-media-va-driver-non-free,mesa-utils,python3,python3-pip,python3-gst-1.0,python3-evdev,xserver-xorg-core,xserver-xorg-video-intel,xserver-xorg-input-libinput,libinput-tools,unclutter,x11-xserver-utils,fonts-dejavu-core,console-setup,keyboard-configuration,dbus-user-session,polkitd,libspa-0.2-bluetooth,pipewire,wireplumber,python3-websocket,python3-aiohttp,python3-asyncio,less,vim-tiny,whiptail,locales,live-boot,live-config,live-config-systemd,live-tools,debootstrap,squashfs-tools \
    "${DEBIAN_VERSION}" \
    "${CHROOT_DIR}" \
    "${MIRROR}"

ok "Base Debian system created"

# =============================================================================
# STEP 3: Mount pseudo-filesystems
# =============================================================================
info "Step 3/10: Setting up chroot environment..."

mount --bind /dev  "${CHROOT_DIR}/dev"
mount --bind /proc "${CHROOT_DIR}/proc"
mount --bind /sys  "${CHROOT_DIR}/sys"
mount --bind /run  "${CHROOT_DIR}/run"

# Clean up on exit
cleanup() {
    info "Cleaning up mount points..."
    umount -l "${CHROOT_DIR}/dev"  2>/dev/null || true
    umount -l "${CHROOT_DIR}/proc" 2>/dev/null || true
    umount -l "${CHROOT_DIR}/sys"  2>/dev/null || true
    umount -l "${CHROOT_DIR}/run"  2>/dev/null || true
}
trap cleanup EXIT

ok "Chroot environment ready"

# =============================================================================
# STEP 4: Configure base system
# =============================================================================
info "Step 4/10: Configuring base system..."

# Hostname
echo "sp7-monitor" > "${CHROOT_DIR}/etc/hostname"
cat > "${CHROOT_DIR}/etc/hosts" << 'EOF'
127.0.0.1   localhost
127.0.1.1   sp7-monitor
EOF

# Locale
echo "en_US.UTF-8 UTF-8" > "${CHROOT_DIR}/etc/locale.gen"
chroot "${CHROOT_DIR}" locale-gen en_US.UTF-8

# Apt sources
cat > "${CHROOT_DIR}/etc/apt/sources.list" << EOF
deb http://deb.debian.org/debian ${DEBIAN_VERSION} main contrib non-free non-free-firmware
deb http://security.debian.org/debian-security ${DEBIAN_VERSION}-security main contrib non-free non-free-firmware
deb http://deb.debian.org/debian ${DEBIAN_VERSION}-updates main contrib non-free non-free-firmware
EOF

# Add linux-surface repository
cat > "${CHROOT_DIR}/etc/apt/sources.list.d/linux-surface.list" << 'EOF'
deb [arch=amd64 signed-by=/usr/share/keyrings/linux-surface-archive-keyring.gpg] https://pkg.surfacelinux.com/debian release main
EOF

# Add linux-surface GPG key
chroot "${CHROOT_DIR}" bash -c '
    mkdir -p /usr/share/keyrings
    curl -fsSL https://raw.githubusercontent.com/linux-surface/linux-surface/master/pkg/keys/surface-archive-keyring.gpg \
        -o /usr/share/keyrings/linux-surface-archive-keyring.gpg
'

# Update and install surface packages
chroot "${CHROOT_DIR}" apt-get update -qq
chroot "${CHROOT_DIR}" apt-get install -y -qq \
    linux-headers-surface \
    linux-image-surface \
    libwacom-surface \
    iptsd \
    2>&1 | tail -5

ok "linux-surface kernel and iptsd installed"

# =============================================================================
# STEP 5: Install receiver application dependencies
# =============================================================================
info "Step 5/10: Installing receiver dependencies..."

chroot "${CHROOT_DIR}" pip3 install --break-system-packages msgpack websockets aiohttp 2>&1 | tail -3

ok "Python dependencies installed"

# =============================================================================
# STEP 6: Install receiver application and configs
# =============================================================================
info "Step 6/10: Installing receiver application..."

# Copy receiver app
cp -r "${RECEIVER_DIR}"/* "${CHROOT_DIR}/usr/local/bin/"
chmod +x "${CHROOT_DIR}/usr/local/bin/sp7-receiver.py"
chmod +x "${CHROOT_DIR}/usr/local/bin/sp7-input-client.py"
chmod +x "${CHROOT_DIR}/usr/local/bin/sp7-monitor"

# Copy overlay files
rsync -a "${OVERLAY_DIR}/" "${CHROOT_DIR}/"

# Make systemd service enabled
chroot "${CHROOT_DIR}" systemctl enable sp7-monitor

ok "Receiver application installed"

# =============================================================================
# STEP 7: Final chroot configuration
# =============================================================================
info "Step 7/10: Final configuration..."

# Update initramfs to include surface modules
chroot "${CHROOT_DIR}" update-initramfs -u -k all

# Ensure surface modules are loaded
cat >> "${CHROOT_DIR}/etc/modules" << 'EOF'
# Surface Pro 7 hardware
intel_ipts
ithc
surface_aggregator
surface_hid
EOF

# Blacklist problematic modules
cat > "${CHROOT_DIR}/etc/modprobe.d/sp7-blacklist.conf" << 'EOF'
# Prevent iwlwifi power save for low latency
options iwlwifi power_save=0 uapsd_disable=1
options iwlmvm power_scheme=1
EOF

# Create user
chroot "${CHROOT_DIR}" useradd -m -s /bin/bash -G video,audio,input,dialout,netdev monitor

# Set root password for emergency access
echo 'root:monitor' | chroot "${CHROOT_DIR}" chpasswd

ok "Configuration complete"

# =============================================================================
# STEP 8: Build squashfs
# =============================================================================
info "Step 8/10: Building squashfs root filesystem..."

rm -rf "${ISO_DIR}"
mkdir -p "${ISO_DIR}/live"

# Clean up before squashfs
chroot "${CHROOT_DIR}" apt-get clean
chroot "${CHROOT_DIR}" rm -rf /var/cache/apt/archives/* /var/lib/apt/lists/* /tmp/* /var/tmp/*
find "${CHROOT_DIR}/var/log" -type f -delete 2>/dev/null || true

# Build squashfs
mksquashfs "${CHROOT_DIR}" "${ISO_DIR}/live/filesystem.squashfs" \
    -comp xz \
    -e boot \
    -noappend \
    -wildcards \
    2>&1 | tail -3

# Copy kernel and initramfs
mkdir -p "${ISO_DIR}/live"
cp "${CHROOT_DIR}/boot/vmlinuz-"*surface* "${ISO_DIR}/live/vmlinuz" 2>/dev/null || \
cp "${CHROOT_DIR}/boot/vmlinuz-"* "${ISO_DIR}/live/vmlinuz"
cp "${CHROOT_DIR}/boot/initrd.img-"*surface* "${ISO_DIR}/live/initrd.img" 2>/dev/null || \
cp "${CHROOT_DIR}/boot/initrd.img-"* "${ISO_DIR}/live/initrd.img"

ISO_SIZE=$(du -sh "${ISO_DIR}/live/filesystem.squashfs" | cut -f1)
ok "Squashfs built (${ISO_SIZE})"

# =============================================================================
# STEP 9: Create EFI boot image and GRUB config
# =============================================================================
info "Step 9/10: Creating boot configuration..."

# EFI boot partition image
mkdir -p "${ISO_DIR}/boot/grub"
mkdir -p "${ISO_DIR}/EFI/BOOT"

# GRUB config
cat > "${ISO_DIR}/boot/grub/grub.cfg" << 'EOF'
set timeout=3
set default=0

# Load video drivers
insmod efi_gop
insmod efi_uga
insmod font
insmod gfxterm
insmod videotest
insmod videoinfo

# Set resolution for Surface Pro 7 (2736x1824 not always available, try common)
set gfxmode=auto
set gfxpayload=keep

# Terminal output
terminal_output gfxterm

# Theme
set menu_color_normal=white/black
set menu_color_highlight=black/white

menuentry "SP7 Wireless Monitor (Live)" --class debian --class gnu-linux --class gnu --class os {
    linux /live/vmlinuz boot=live components quiet splash
    initrd /live/initrd.img
}

menuentry "SP7 Wireless Monitor (Debug - verbose)" --class debian --class gnu-linux --class gnu --class os {
    linux /live/vmlinuz boot=live components verbose debug nosplash systemd.log_level=debug systemd.log_target=console
    initrd /live/initrd.img
}

menuentry "SP7 Wireless Monitor (Failsafe)" --class debian --class gnu-linux --class gnu --class os {
    linux /live/vmlinuz boot=live components nomodeset acpi=off noapic nolapic
    initrd /live/initrd.img
}
EOF

# Create embedded early config — tells GRUB to search the ISO9660 tree
# for grub.cfg, since grub.cfg is NOT on the ESP (efiboot.img) but
# in the main ISO9660 filesystem.
mkdir -p "${BUILD_DIR}"
cat > "${BUILD_DIR}/early.cfg" << EOF
# SP7 Wireless Monitor - GRUB early embedded config
# Search for the ISO volume label, set it as root, then load the real grub.cfg
search --no-floppy --set=root --label "${ISO_LABEL}"
set prefix=(\$root)/boot/grub
configfile (\$root)/boot/grub/grub.cfg
EOF

# Copy GRUB EFI binary, or build one with embedded early config
cp /usr/lib/grub/x86_64-efi-signed/gcdx64.efi.signed "${ISO_DIR}/EFI/BOOT/BOOTX64.EFI" 2>/dev/null || \
cp /usr/lib/shim/shimx64.efi.signed "${ISO_DIR}/EFI/BOOT/BOOTX64.EFI" 2>/dev/null || \
grub-mkimage \
    --format=x86_64-efi \
    --output="${ISO_DIR}/EFI/BOOT/BOOTX64.EFI" \
    --prefix=/boot/grub \
    --config="${BUILD_DIR}/early.cfg" \
    efi_gop efi_uga fat iso9660 part_gpt part_msdos \
    normal boot linux configfile loopback chain \
    efifwsetup efi_net keystatus gfxmenu regexp \
    gfxterm all_video font read echo file test \
    multiboot search search_fs_file search_fs_uuid \
    search_label xfs gzio lvm ls reboot halt help \
    video_colors video_bochs video_cirrus ext2 btrfs

ok "Boot configuration created"
info "GRUB embedded early config: ${BUILD_DIR}/early.cfg"

# ---------------------------------------------------------------------------
# Create GRUB boot image for BIOS booting
# ---------------------------------------------------------------------------
info "Creating GRUB boot image..."

# Create i386-pc boot image (for legacy BIOS boot path)
# Surface Pro 7 is UEFI-only, but this is kept as a compatibility fallback
mkdir -p "${ISO_DIR}/boot/grub/i386-pc"
grub-mkimage \
    --format=i386-pc-eltorito \
    --output="${ISO_DIR}/boot/grub/grub.img" \
    --prefix=/boot/grub \
    --compression=xz \
    biosdisk iso9660 normal boot linux configfile loopback chain \
    gfxterm gfxmenu all_video font read echo file test \
    multiboot search search_fs_file search_fs_uuid search_label \
    xfs gzio lvm ls reboot halt help \
    video_colors video_bochs video_cirrus ext2 btrfs

ok "GRUB BIOS boot image created"

# ---------------------------------------------------------------------------
# Create FAT EFI System Partition (ESP) image for UEFI USB boot
# UEFI firmware boots USB by finding a FAT partition and reading
# \EFI\BOOT\BOOTX64.EFI from it. A raw executable as the partition
# content won't mount — we need a proper FAT filesystem image.
# ---------------------------------------------------------------------------
info "Creating EFI System Partition (FAT) image..."

# Create empty FAT32 image (33MB — minimum for spec-compliant FAT32,
# which needs >=65525 clusters. 16MB is too small and can fail on
# strict UEFI firmware like the Surface Pro 7's.)
dd if=/dev/zero of="${ISO_DIR}/efiboot.img" bs=1M count=33 status=none
mkfs.fat -n "SP7_EFI" -F 32 "${ISO_DIR}/efiboot.img" > /dev/null

# Create directory structure inside the FAT image
mmd -i "${ISO_DIR}/efiboot.img" ::/EFI
mmd -i "${ISO_DIR}/efiboot.img" ::/EFI/BOOT

# Copy EFI bootloader into the FAT image at the standard path
mcopy -i "${ISO_DIR}/efiboot.img" \
    "${ISO_DIR}/EFI/BOOT/BOOTX64.EFI" \
    ::/EFI/BOOT/BOOTX64.EFI

ok "FAT EFI System Partition image created (efiboot.img)"

# =============================================================================
# STEP 10: Build ISO
# =============================================================================
info "Step 10/10: Building bootable ISO..."

rm -f "${ISO_OUTPUT}"

xorriso -as mkisofs \
    -iso-level 3 \
    -full-iso9660-filenames \
    -volid "${ISO_LABEL}" \
    -eltorito-boot boot/grub/grub.img \
    -no-emul-boot \
    -boot-load-size 4 \
    -boot-info-table \
    --eltorito-catalog boot/grub/boot.cat \
    -eltorito-alt-boot \
    -e efiboot.img \
    -no-emul-boot \
    -isohybrid-gpt-basdat \
    -isohybrid-apm-hfsplus \
    -output "${ISO_OUTPUT}" \
    "${ISO_DIR}" \
    2>&1 | tail -10

# Verify ISO
if [ -f "${ISO_OUTPUT}" ]; then
    ISO_SIZE=$(du -sh "${ISO_OUTPUT}" | cut -f1)
    ok "========================================"
    ok "  ISO build complete!"
    ok "========================================"
    info ""
    info "Output: ${ISO_OUTPUT}"
    info "Size:   ${ISO_SIZE}"
    info ""
    info "Flash to USB with:"
    info "  dd if=${ISO_NAME} of=/dev/sdX bs=4M status=progress"
    info "  # OR use balenaEtcher, Rufus, etc."
    info ""
    info "Boot on Surface Pro 7:"
    info "  1. Hold Volume Down + press Power to boot from USB"
    info "  2. Select 'SP7 Wireless Monitor (Live)' from GRUB menu"
    info "  3. The receiver will auto-start and discover your host"
    info ""
    info "NOTE: Disable Secure Boot in Surface UEFI settings"
    info "      (hold Power + Volume Up at boot to enter UEFI)"
else
    error "ISO creation failed"
fi
