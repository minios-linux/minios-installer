EXECUTABLES = minios-installer
APPLICATIONS = minios-installer.desktop
POLICIES = dev.minios.installer.policy
STYLES = style.css

BINDIR = usr/bin
APPLICATIONSDIR = usr/share/applications
POLKITACTIONSDIR = usr/share/polkit-1/actions
LOCALEDIR = usr/share/locale
SHAREDIR = usr/share/minios-installer

PO_FILES = $(shell find po -name "*.po" -maxdepth 1)
MO_FILES = $(patsubst %.po,%.mo,$(PO_FILES))

build: mo

mo: $(MO_FILES)

%.mo: %.po
	@echo "Generating mo file for $<"
	msgfmt -o $@ $<
	chmod 644 $@

clean:
	rm -rf $(MO_FILES)

install: build
	install -d $(DESTDIR)/$(BINDIR) \
				$(DESTDIR)/$(APPLICATIONSDIR) \
				$(DESTDIR)/$(POLKITACTIONSDIR) \
				$(DESTDIR)/$(LOCALEDIR) \
				$(DESTDIR)/$(SHAREDIR)

	cp $(EXECUTABLES) $(DESTDIR)/$(BINDIR)
	cp $(APPLICATIONS) $(DESTDIR)/$(APPLICATIONSDIR)
	cp $(POLICIES) $(DESTDIR)/$(POLKITACTIONSDIR)
	cp $(STYLES) $(DESTDIR)/$(SHAREDIR)

	for MO_FILE in $(MO_FILES); do \
		LOCALE=$$(basename $$MO_FILE .mo); \
		echo "Copying mo file $$MO_FILE to $(DESTDIR)/usr/share/locale/$$LOCALE/LC_MESSAGES/minios-installer.mo"; \
		install -Dm644 "$$MO_FILE" "$(DESTDIR)/usr/share/locale/$$LOCALE/LC_MESSAGES/minios-installer.mo"; \
	done
