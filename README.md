# MiniOS Installer

GTK3 wizard and command-line backend for installing MiniOS from a live session.

## Installation Modes

- **Live** copies the selected MiniOS module stack and boot assets while keeping
  the modular live-system layout.
- **Native** deploys the module stack into a conventional Linux root filesystem,
  configures the target, installs required packages, generates initramfs, and
  installs the supported bootloader.

The installer detects native-install capabilities from the booted image rather
than from its release number. If the image does not provide the required format-1
kernel metadata and EFI architecture contract, compatibility mode is enabled
automatically and only **Live** installation is available.

`minios-deploy` depends on `minios-native-dracut`, which supplies `dracut-core`
and guarded kernel hooks for a conventional native initramfs. The hooks remain
inactive in the live system and are enabled only in a completed native target,
so native installation works the same whether the live image uses dracut-mos or
livekit-mos.

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
- Native, DynFileFS, DynBlk, raw-image, or LUKS live-session persistence
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
thin DynBlk storage, and fixed-size raw images. Raw, DynFileFS, and DynBlk may
optionally use LUKS2 encryption. During installation, `minios-deploy` calls the
`minios-session` backend to create storage in the target partition's
`minios/changes` directory and select it as the boot default. Both
`session.json` and `session.conf` are published by the session backend. The
running system's sessions are not copied or switched. No persistence kernel
parameters are generated or rewritten.

Raw and DynFileFS default to 4000 MiB; DynBlk defaults to 16 GiB. Only Raw
is limited to 4000 MiB on FAT32. Native persistence is offered only on
POSIX-compatible target filesystems.

For non-encrypted DynBlk storage, the installer can select `none`, `lz4`,
`lz4hc`, `lzo`, `lzo-rle`, `zstd`, `deflate`, or `842` compression. It passes
that choice through `minios-session create --compression` when creating the
container. DynBlk compression is not offered when LUKS is selected.

LUKS is offered only when the running initrd marker contains
`luks-layer-v1` and the selected backend is available. Every copied source
initrd is inspected and verified again before disk changes. The installer asks
for and confirms the LUKS passphrase before changing the disk, then sends it to
`minios-session` only over stdin. It is not written to arguments, logs, session
metadata, or configuration files. Boot only unlocks the already-created session.
For unattended CLI use, `--persistence-password-stdin` reads the passphrase and
confirmation from two stdin lines. Plans and dry runs do not read passwords or
create sessions. Session creation is optional: `minios-deploy` lists
`minios-session >= 2.2.0` in `Suggests`, not `Depends`. Without the `minios-session`
executable, the wizard hides all session-creation controls and clears their
previous settings. The CLI hides persistence options from help and completion;
an explicit session-creation request fails before disk changes with an instruction
to install `minios-session`. Live installation without a session and native
installation remain available.

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
