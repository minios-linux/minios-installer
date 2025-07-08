# MiniOS Installer

A graphical tool for installing MiniOS permanently to your flash drive.

## Features

- GTK3 interface with multi-language support
- Multiple filesystem support (ext4, btrfs, fat32, etc.)
- UEFI/BIOS compatibility

## Usage

Launch the installer:
```bash
sudo minios-installer
```

Or from Applications Menu: System → MiniOS Installer

## Build

```bash
make build
```

## Install

```bash
sudo make install
```

## Translation

Update translations:
```bash
make update-po
```

## License

GPL-3.0