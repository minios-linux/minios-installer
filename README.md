# MiniOS Installer

## Overview

GTK3 wizard and command-line backend for installing MiniOS from a live session.

## Features

- Module selection with required lower layers included automatically
- Required-space calculation from selected data, boot assets, persistence, and a 25 percent filesystem reserve
- Automatic, BIOS/MBR, UEFI/MBR, and UEFI/GPT boot-layout choices
- Security profiles (`convenient`, `balanced`, `strict`) with independent SSH and XRDP service controls
- Hostname and wired DHCP or static IPv4 configuration; Wi-Fi profiles are left unchanged
- Locale, timezone, keyboard, user, password, service, and boot-menu setup
- Native, DynFileFS, DynBlk, raw-image, or LUKS live-session persistence
- Stable device identity, exact geometry previews, package preflight, and final destructive confirmation

Live layouts support ext2, ext4, btrfs, FAT32, and NTFS when their tools are installed. Native roots support ext2, ext4, and btrfs. Ext3 filesystems may be reused or shrunk where supported but are not offered as a new-format choice.

## Installation Modes

- **Live** copies the selected MiniOS module stack and boot assets while keeping the modular live-system layout.
- **Native** deploys the module stack into a conventional Linux root filesystem, configures the target, installs required packages, generates initramfs, and installs the supported bootloader.

The installer detects native-install capabilities from the booted image rather than from its release number. If the image does not provide the required format-1 kernel metadata and EFI architecture contract, compatibility mode is enabled automatically and only **Live** installation is available.

`minios-deploy` depends on `minios-native-dracut`, which supplies `dracut-core` and guarded kernel hooks for a conventional native initramfs. The hooks remain inactive in the live system and are enabled only in a completed native target, so native installation works the same whether the live image uses dracut-mos or livekit-mos.

Placement choices are erase-all, existing free space, alongside a supported final partition, and GUI manual partitioning for native installations.

## Manual Partitioning

Manual partitioning is available only for native GUI installations on eligible direct disks. All changes are staged until final confirmation. Supported actions are create, delete, end-only ext2/ext3/ext4 or NTFS shrink, format, reuse, mount-point assignment, ESP assignment, swap assignment, undo, and reset.

Manual mode supports GPT with UEFI and primary MBR with UEFI or BIOS. BIOS on GPT is rejected because there is no `bios_grub` installation path. Extended or logical MBR partitions, LVM, RAID, LUKS, mapped/nested storage, bcache, ZFS, and Btrfs subvolume editing are unsupported. LUKS persistence is a live-session container feature, not native root encryption.

## Alongside Installation

Alongside mode can shrink an eligible unmounted final ext2/ext3/ext4 or NTFS partition. Filesystem checks and shrink complete before the partition boundary is changed. Missing resize tools are installed only after explicit consent. Unsupported, mounted, nested, dirty, ambiguous, or unsafe layouts are refused.

## Session Persistence

Live installations support native directories, expandable DynFileFS storage, thin DynBlk storage, and fixed-size raw images. Raw, DynFileFS, and DynBlk may optionally use LUKS2 encryption. During installation, `minios-deploy` calls the `minios-session` backend to create storage in the target partition's `minios/changes` directory and select it as the boot default. Both `session.json` and `session.conf` are published by the session backend. The running system's sessions are not copied or switched. No persistence kernel parameters are generated or rewritten.

Raw and DynFileFS default to 4000 MiB; DynBlk defaults to 16 GiB. Only Raw is limited to 4000 MiB on FAT32. Native persistence is offered only on POSIX-compatible target filesystems.

For non-encrypted DynBlk storage, the installer offers only compression codecs that can be used both by the currently running kernel/initrd (which creates the container) and by every kernel/initrd copied to the target (which must reopen it later). Support is derived from kmod metadata and the Linux `crypto_comp` API, including built-in providers and module dependencies; no codec capability marker is used. Runtime probing uses the running system module tree because LiveKit does not retain modules in `/run/initramfs` after boot. Source initrds are independently unpacked with `unmkinitramfs` or Dracut's `lsinitrd --unpack`, with symlinked image paths resolved first. Known codecs are `none`, `lz4`, `lz4hc`, `lzo`, `lzo-rle`, `zstd`, `deflate`, and `842`; unavailable codecs are hidden and rejected if explicitly requested. The selected codec is passed through `minios-session create --compression`. DynBlk compression is not offered when LUKS is selected.

LUKS is offered only when the running initrd marker contains `luks-layer-v1` and the selected backend is available. Every copied source initrd is inspected and verified again before disk changes. The installer asks for and confirms the LUKS passphrase before changing the disk, then sends it to `minios-session` only over stdin. It is not written to arguments, logs, session metadata, or configuration files. Boot only unlocks the already-created session. For unattended CLI use, `--persistence-password-stdin` reads the passphrase and confirmation from two stdin lines. Plans and dry runs do not read passwords or create sessions. Session creation is optional: `minios-deploy` lists `minios-session >= 2.2.0` in `Suggests`, not `Depends`. Without the `minios-session` executable, the wizard hides all session-creation controls and clears their previous settings. The CLI hides persistence options from help and completion; an explicit session-creation request fails before disk changes with an instruction to install `minios-session`. Live installation without a session and native installation remain available.

## DynBlk capacity

The DynBlk limit is obtained from dynblk limits, not a fixed 512-GiB ceiling. Large thin containers also require space for the metadata of all declared parts. Raw and DynFileFS retain their separate capacity policies.

## DynBlk and split VMDK persistence

Live installation supports `--persistence-mode dynblk` (native compressed container) and `--persistence-mode vmdk` (standard split sparse VMDK without compression). Both default to 16384 MiB and query their installed driver limits. The GUI presents them separately; only the native format offers compression. LUKS2 is a separate optional layer for both formats.

Creation uses the shared `minios-session` CLI on the target medium, not private installer image-format code. VMDK is offered only with the corresponding runtime capability and rejected before installation when any copied source initrd lacks `vmdk-session-v1`. Old initrds must not be used to boot new VMDK session records. Without the optional session-management package, storage creation controls remain hidden.

## Usage

```bash
minios-installer
minios-deploy list-disks
minios-deploy plan /dev/sdb --mode native --placement free_space
```

See `minios-installer(1)` and `minios-deploy(1)` for complete behavior and limitations.

## Development

```bash
make build
sudo make install
```

Runtime dependencies and operation-specific recommendations are defined in `debian/control`. Native and alongside installations may require APT access to stage GRUB, EFI, initramfs, `os-prober`, and resize packages before disk changes.

## License

GPL-3.0+

### Session controls and additional modules

Session encryption and compression rows are hidden when they do not apply to
the selected backend; selecting LUKS hides DynBlk compression. Supported codecs
are the intersection of the running kernel and the actual source initrds.
The shutdown-only `/run/initramfs` tree is not a codec inventory.

The Modules page includes `.sb` files from the media root followed by files
recursively below `minios/modules/`, in boot layer order. Selection, space
estimation and live copying use that same inventory. Unselected module files
are not copied; selected custom modules keep their relative subdirectories.
