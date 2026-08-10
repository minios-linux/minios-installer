# MiniOS Installer 3.0.2

GTK3 wizard and command-line backend for installing MiniOS from a live session.

## Installation Modes

- **Live** copies the selected MiniOS module stack and boot assets while keeping
  the modular live-system layout.
- **Native** deploys the module stack into a conventional Linux root filesystem,
  configures the target, installs required packages, generates initramfs, and
  installs the supported bootloader.

Placement choices are erase-all, existing free space, alongside a supported
final partition, and GUI manual partitioning for native installations.

## Features

- Module selection with required lower layers included automatically
- Required-space calculation from selected data, boot assets, persistence, and
  a 25 percent filesystem reserve
- Automatic, BIOS/MBR, UEFI/MBR, and UEFI/GPT boot-layout choices
- Security profiles (`convenient`, `balanced`, `strict`) with independent SSH
  and XRDP service controls
- Hostname and wired DHCP or static IPv4 configuration; Wi-Fi profiles are left
  unchanged
- Locale, timezone, keyboard, user, password, service, and boot-menu setup
- Native, DynFileFS, raw-image, or LUKS live-session persistence
- Stable device identity, exact geometry previews, package preflight, and final
  destructive confirmation

Live layouts support ext2, ext4, btrfs, FAT32, and NTFS when their tools are
installed. Native roots support ext2, ext4, and btrfs. Ext3 filesystems may be
reused or shrunk where supported but are not offered as a new-format choice.

## Manual Partitioning

Manual partitioning is available only for native GUI installations on eligible
direct disks. All changes are staged until final confirmation. Supported actions
are create, delete, end-only ext2/ext3/ext4 or NTFS shrink, format, reuse,
mount-point assignment, ESP assignment, swap assignment, undo, and reset.

Manual mode supports GPT with UEFI and primary MBR with UEFI or BIOS. BIOS on
GPT is rejected because there is no `bios_grub` installation path. Extended or
logical MBR partitions, LVM, RAID, LUKS, mapped/nested storage, bcache, ZFS, and
Btrfs subvolume editing are unsupported. LUKS persistence is a live-session
container feature, not native root encryption.

## Alongside Installation

Alongside mode can shrink an eligible unmounted final ext2/ext3/ext4 or NTFS
partition. Filesystem checks and shrink complete before the partition boundary
is changed. Missing resize tools are installed only after explicit consent.
Unsupported, mounted, nested, dirty, ambiguous, or unsafe layouts are refused.

## Session Persistence

Live installations support native directories, expandable DynFileFS storage,
fixed-size raw images, and encrypted LUKS images. The initrd creates the selected
storage on first boot. Container modes default to 4000 MiB. Raw and LUKS are
limited to 4000 MiB on FAT32; DynFileFS is not subject to that single-file
limit. Native persistence is offered only on POSIX-compatible target filesystems.

LUKS is offered only when the running initrd advertises
`/run/initramfs/etc/minios-initramfs-crypt`; every copied source initrd is
verified again before disk changes. The initrd creates `changes.luks` and asks
for the passphrase on boot; the installer never receives or stores it.

## Usage

```bash
minios-installer
minios-deploy list-disks
minios-deploy plan /dev/sdb --mode native --placement free_space
```

See `minios-installer(1)` and `minios-deploy(1)` for complete behavior and
limitations.

## Build

```bash
make build
sudo make install
```

Runtime dependencies and operation-specific recommendations are defined in
`debian/control`. Native and alongside installations may require APT access to
stage GRUB, EFI, initramfs, `os-prober`, and resize packages before disk changes.

## License

GPL-3.0+
