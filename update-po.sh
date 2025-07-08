#!/bin/bash

# Universal translation management script
# Automatically detects project info and manages translations

SCRIPT_DIR="$(dirname "$(readlink -f "${0}")")"
cd "$SCRIPT_DIR"

# Auto-detect configuration
if [ -f "debian/changelog" ]; then
    APPNAME=$(head -n1 debian/changelog | cut -d' ' -f1)
    VERSION=$(head -n1 debian/changelog | sed 's/.*(\([^)]*\)).*/\1/')
else
    APPNAME=$(basename "$PWD")
    VERSION="unknown"
fi

POTFILE="po/messages.pot"

# Configuration
BUGS_EMAIL="support@minios.dev"
COPYRIGHT_HOLDER="MiniOS Linux"

# Auto-detect languages from existing .po files
if [ -d "po" ]; then
    LANGUAGES=($(find po -maxdepth 1 -name "*.po" -exec basename {} .po \; | sort))
fi

# Default languages if none found
if [ ${#LANGUAGES[@]} -eq 0 ]; then
    LANGUAGES=("ru" "pt" "pt_BR" "it" "id" "fr" "es" "de")
fi

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Helper functions
log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

log_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

show_stats() {
    if [ ! -f "$POTFILE" ]; then
        log_warning "Translation template not found, skipping statistics"
        return 1
    fi
    
    log_info "Translation statistics for $APPNAME:"
    
    # Get total number of messages
    local total=$(msgcat "$POTFILE" | grep -c '^msgid ' 2>/dev/null || echo "0")
    
    if [ "$total" -eq 0 ]; then
        log_warning "No translatable messages found"
        return 1
    fi
    
    printf "%-8s %-12s %-12s %-8s\n" "Lang" "Translated" "Fuzzy" "Percent"
    echo "----------------------------------------"
    
    for lang in "${LANGUAGES[@]}"; do
        local pofile="po/${lang}.po"
        
        if [ -f "$pofile" ]; then
            local stats=$(msgfmt --statistics -o /dev/null "$pofile" 2>&1)
            local translated=$(echo "$stats" | grep -o '[0-9]\+ translated' | grep -o '[0-9]\+' || echo "0")
            local fuzzy=$(echo "$stats" | grep -o '[0-9]\+ fuzzy' | grep -o '[0-9]\+' || echo "0")
            local percent=0
            
            if [ "$total" -gt 0 ]; then
                percent=$((translated * 100 / total))
            fi
            
            printf "%-8s %-12s %-12s %-8s%%\n" "$lang" "$translated" "$fuzzy" "$percent"
        else
            printf "%-8s %-12s %-12s %-8s\n" "$lang" "missing" "-" "-"
        fi
    done
    
    echo "----------------------------------------"
    log_info "Total messages: $total"
    
    # Show summary of issues
    local has_issues=false
    for lang in "${LANGUAGES[@]}"; do
        local pofile="po/${lang}.po"
        if [ -f "$pofile" ]; then
            local untranslated_file=$(msgattrib --untranslated "$pofile" 2>/dev/null)
            local fuzzy_file=$(msgattrib --only-fuzzy "$pofile" 2>/dev/null)
            
            local untranslated=0
            local fuzzy=0
            
            if [ -n "$untranslated_file" ]; then
                untranslated=$(echo "$untranslated_file" | grep -c '^msgid ' 2>/dev/null || echo "0")
            fi
            
            if [ -n "$fuzzy_file" ]; then
                fuzzy=$(echo "$fuzzy_file" | grep -c '^msgid ' 2>/dev/null || echo "0")
            fi
            
            if [ "$untranslated" -gt 0 ] || [ "$fuzzy" -gt 0 ]; then
                if [ "$has_issues" = false ]; then
                    echo
                    log_info "Translation issues found:"
                    has_issues=true
                fi
                
                local issues=""
                [ "$untranslated" -gt 0 ] && issues="$untranslated untranslated"
                [ "$fuzzy" -gt 0 ] && issues="$issues${issues:+, }$fuzzy fuzzy"
                
                echo "  $lang: $issues"
            fi
        fi
    done
    
    if [ "$has_issues" = true ]; then
        echo
        log_info "Use 'msgattrib --untranslated po/LANG.po' to see untranslated strings"
        log_info "Use 'msgattrib --only-fuzzy po/LANG.po' to see fuzzy translations"
    fi
}

generate_pot() {
    log_info "Generating translation template for $APPNAME..."
    
    # Auto-detect source files
    local sources=""
    
    # Look for Python files
    if [ -d "lib" ] || [ -d "bin" ] || [ -d "src" ]; then
        sources=$(find lib bin src -type f -name "*.py" 2>/dev/null | tr '\n' ' ')
    fi
    
    # Look for shell scripts if no Python files found
    if [ -z "$sources" ]; then
        sources=$(find . -maxdepth 2 -type f -name "*.sh" 2>/dev/null | tr '\n' ' ')
    fi
    
    # Look for any executable scripts
    if [ -z "$sources" ]; then
        sources=$(find . -maxdepth 2 -type f -executable 2>/dev/null | grep -v '\./' | tr '\n' ' ')
    fi
    
    if [ -z "$sources" ]; then
        log_error "No source files found for translation"
        return 1
    fi
    
    log_info "Found source files: $sources"
    
    # Create po directory
    mkdir -p po
    
    # Extract translatable strings
    if xgettext --language=Python \
                --keyword=_ \
                --keyword=N_ \
                --add-comments=TRANSLATORS \
                --from-code=UTF-8 \
                --package-name="$APPNAME" \
                --package-version="$VERSION" \
                --msgid-bugs-address="$BUGS_EMAIL" \
                --copyright-holder="$COPYRIGHT_HOLDER" \
                --output="$POTFILE" \
                $sources 2>/dev/null; then
        log_success "Translation template generated: $POTFILE"
        return 0
    else
        log_error "Failed to generate translation template"
        return 1
    fi
}

update_po_files() {
    log_info "Updating .po files for languages: ${LANGUAGES[*]}"
    
    if [ ! -f "$POTFILE" ]; then
        log_error "Translation template not found: $POTFILE"
        return 1
    fi
    
    local updated=0
    local created=0
    
    for lang in "${LANGUAGES[@]}"; do
        local pofile="po/${lang}.po"
        
        if [ ! -f "$pofile" ]; then
            log_warning "Creating new translation file for $lang..."
            if msginit --input="$POTFILE" --locale="$lang" --output-file="$pofile" --no-translator 2>/dev/null; then
                log_success "Created: $pofile"
                ((created++))
            else
                log_error "Failed to create: $pofile"
            fi
        else
            if msgmerge --update --backup=off "$pofile" "$POTFILE" 2>/dev/null; then
                log_success "Updated: $pofile"
                ((updated++))
            else
                log_error "Failed to update: $pofile"
            fi
        fi
    done
    
    if [ $created -gt 0 ] || [ $updated -gt 0 ]; then
        log_info "Summary: $created created, $updated updated"
    else
        log_warning "No files were created or updated"
    fi
}

# Main execution
log_info "Starting translation workflow for $APPNAME (version $VERSION)..."
log_info "Languages: ${LANGUAGES[*]}"

if generate_pot; then
    update_po_files
    show_stats
    log_success "Translation workflow completed!"
else
    log_error "Workflow failed during template generation"
    exit 1
fi
