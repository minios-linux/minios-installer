# minios-installer

MiniOS Installer - a graphical tool for installing MiniOS on a disk.

## Build and Translation

### Generating Translation Template

To create or update the translation template (messages.pot):

```bash
make makepot
```

or directly:

```bash
./makepot
```

### Updating Translations

To update all translation files (.po):

```bash
make update-po
```

or directly:

```bash
./update_translations.sh
```

### Build

To build the project with .mo file generation:

```bash
make build
```

### Clean

To remove generated .mo files:

```bash
make clean
```

### Installation

```bash
make install DESTDIR=/path/to/destination
```