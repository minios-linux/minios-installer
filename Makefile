EXECUTABLES = bin/minios-installer
LIBRARIES = lib/*.py
APPLICATIONS = share/applications/minios-installer.desktop
POLICIES = share/polkit/org.minios.installer.policy
STYLES = share/styles/style.css

BINDIR = usr/bin
LIBDIR = usr/lib/minios-installer
APPLICATIONSDIR = usr/share/applications
POLKITACTIONSDIR = usr/share/polkit-1/actions
LOCALEDIR = usr/share/locale
SHAREDIR = usr/share/minios-installer

PO_FILES = $(shell find po -maxdepth 1 -name "*.po")
MO_FILES = $(patsubst %.po,%.mo,$(PO_FILES))

build: mo

mo: $(MO_FILES)

makepot:
	@echo "Generating translation template..."
	./makepot

update-po: makepot
	@echo "Updating translation files..."
	./update_translations.sh

%.mo: %.po
	@echo "Generating mo file for $<"
	msgfmt -o $@ $<
	chmod 644 $@

clean:
	rm -rf $(MO_FILES)

install: build
	install -d $(DESTDIR)/$(BINDIR) \
				$(DESTDIR)/$(LIBDIR) \
				$(DESTDIR)/$(APPLICATIONSDIR) \
				$(DESTDIR)/$(POLKITACTIONSDIR) \
				$(DESTDIR)/$(LOCALEDIR) \
				$(DESTDIR)/$(SHAREDIR)

	cp $(EXECUTABLES) $(DESTDIR)/$(BINDIR)/minios-installer
	cp $(LIBRARIES) $(DESTDIR)/$(LIBDIR)/
	chmod +x $(DESTDIR)/$(LIBDIR)/main_installer.py
	cp $(APPLICATIONS) $(DESTDIR)/$(APPLICATIONSDIR)
	cp $(POLICIES) $(DESTDIR)/$(POLKITACTIONSDIR)
	cp $(STYLES) $(DESTDIR)/$(SHAREDIR)

	for MO_FILE in $(MO_FILES); do \
		LOCALE=$$(basename $$MO_FILE .mo); \
		echo "Copying mo file $$MO_FILE to $(DESTDIR)/usr/share/locale/$$LOCALE/LC_MESSAGES/minios-installer.mo"; \
		install -Dm644 "$$MO_FILE" "$(DESTDIR)/usr/share/locale/$$LOCALE/LC_MESSAGES/minios-installer.mo"; \
	done

.PHONY: build mo makepot update-po clean install
