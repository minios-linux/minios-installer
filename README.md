# MiniOS Installer

GTK3 graphical tool for installing MiniOS permanently to disk from a live session.

## Features

- Multiple filesystem support (ext4, btrfs, xfs, f2fs, FAT32)
- UEFI/BIOS compatibility with automatic detection
- Step-by-step installation process with progress tracking
- PolicyKit authentication for secure operations

## Usage

```bash
minios-installer
```

Or from Applications Menu: System → Install MiniOS

## Build

```bash
make build
sudo make install
```

## License

GPL-3.0+

## Author

crims0n <crims0n@minios.dev>