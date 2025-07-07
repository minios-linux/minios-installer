#!/bin/bash
# Define the directory where the script is located (for running from any folder)
SCRIPT_DIR="$(dirname "$(readlink -f "${0}")")"

# Change to script directory to ensure relative paths work correctly
cd "$SCRIPT_DIR"

# Script for updating messages.pot and generating .po translation files
# for the minios-installer

LANGUAGES=("ru" "pt" "pt_BR" "it" "id" "fr" "es" "de")
MESSAGES="po/messages.pot"

echo "Generating translation template using makepot..."
# Use the makepot script to generate the .pot file with Python sources
./makepot

if [ ! -f "$MESSAGES" ]; then
    echo "Error: Failed to generate $MESSAGES"
    exit 1
fi

# For each language
for lang in "${LANGUAGES[@]}"; do
    POFILE="po/${lang}.po"
    if [[ ! -f "$POFILE" ]]; then
        echo "Initializing translation file for language $lang..."
        msginit --input="$MESSAGES" --locale="$lang" --output-file="$POFILE" --no-translator
    else
        echo "Updating translation file for language $lang..."
        msgmerge --update "$POFILE" "$MESSAGES"
    fi
done

echo "PO files generation automation completed."
