#!/usr/bin/env bash
# Idus Elgato Light installer  ·  v1.1  ·  build 2026-10-14
# Installs Idus Elgato Light (GTK4) as a self-contained folder in ~/.local/share/idus-elgato-light
# + a start-menu entry with icon and right-click actions.
# Your lights and presets live in that folder and are never touched by a re-install.
# Run:  bash install-idus-elgato-light.sh          (next to the downloaded files)
#  or:  bash <(curl -fsSL https://github.com/Idus/idus-elgato-light/releases/latest/download/install-idus-elgato-light.sh)
set -e
SRC="$(cd "$(dirname "$0")" 2>/dev/null && pwd || pwd)"
APP="$HOME/.local/share/idus-elgato-light"

# One-line install: without the app next to this script, fetch it from the latest GitHub release
if [ ! -f "$SRC/idus-elgato-light.py" ]; then
    BASE="${IEL_DOWNLOAD_BASE:-https://github.com/Idus/idus-elgato-light/releases/latest/download}"
    command -v curl >/dev/null 2>&1 || { echo "❌ curl is needed — install it with your package manager"; exit 1; }
    TMP="$(mktemp -d)"
    trap 'rm -rf "$TMP"' EXIT
    echo "⬇️  downloading Idus Elgato Light (latest release) …"
    curl -fsSL "$BASE/idus-elgato-light.py" -o "$TMP/idus-elgato-light.py" || \
        { echo "❌ download failed — check your connection, or download the files from GitHub"; exit 1; }
    curl -fsL "$BASE/idus-elgato-light.png" -o "$TMP/idus-elgato-light.png" || true
    head -n 5 "$TMP/idus-elgato-light.py" | grep -q "Idus Elgato Light" || \
        { echo "❌ that download isn't Idus Elgato Light — stopping"; exit 1; }
    SRC="$TMP"
fi
BIN="$HOME/.local/bin"
ICONS="$HOME/.local/share/icons/hicolor/512x512/apps"

mkdir -p "$APP" "$BIN" "$ICONS" ~/.local/share/applications

# App + icon in the same folder (one folder backup captures everything)
install -m 755 "$SRC/idus-elgato-light.py" "$APP/idus-elgato-light.py"
if [ -f "$SRC/idus-elgato-light.png" ]; then
    install -m 644 "$SRC/idus-elgato-light.png" "$APP/idus-elgato-light.png"
    install -m 644 "$SRC/idus-elgato-light.png" "$ICONS/idus-elgato-light.png"
    ICON_LINE="Icon=idus-elgato-light"
else
    echo "⚠️  no idus-elgato-light.png next to the script — using a generic light icon."
    ICON_LINE="Icon=preferences-desktop-display"
fi

# PATH wrapper — OpenDeck commands call this by its full path
{
    echo '#!/usr/bin/env bash'
    echo "exec python3 \"$APP/idus-elgato-light.py\" \"\$@\""
} > "$BIN/idus-elgato-light"
chmod 755 "$BIN/idus-elgato-light"

# Start-menu entry + right-click actions
cat > ~/.local/share/applications/idus-elgato-light.desktop <<DESK
[Desktop Entry]
Type=Application
Name=Idus Elgato Light
Comment=Control Elgato Key Light, Ring Light and Light Strip — presets and OpenDeck shortcuts
Exec=$BIN/idus-elgato-light
$ICON_LINE
Categories=Utility;
Keywords=elgato;key light;ring light;light strip;stream deck;opendeck;light;
StartupWMClass=se.wolfpack.iduselgatolight
Actions=on;off;toggle;

[Desktop Action on]
Name=All lights on
Exec=$BIN/idus-elgato-light --on

[Desktop Action off]
Name=All lights off
Exec=$BIN/idus-elgato-light --off

[Desktop Action toggle]
Name=Toggle all lights
Exec=$BIN/idus-elgato-light --toggle
DESK

# Bust icon & menu caches (NO plasma restart — do that yourself if needed)
command -v gtk-update-icon-cache >/dev/null 2>&1 && \
    gtk-update-icon-cache -f -q "$HOME/.local/share/icons/hicolor" 2>/dev/null || true
command -v update-desktop-database >/dev/null 2>&1 && \
    update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true
rm -f "$HOME/.cache/icon-cache.kcache" 2>/dev/null || true
command -v kbuildsycoca6 >/dev/null 2>&1 && kbuildsycoca6 >/dev/null 2>&1 || \
    { command -v kbuildsycoca5 >/dev/null 2>&1 && kbuildsycoca5 >/dev/null 2>&1; } || true

# Dependency check (GTK4)
if ! python3 -c "import gi; gi.require_version('Gtk','4.0')" 2>/dev/null; then
    echo "⚠️  Missing GTK4 bindings. Run:  sudo pacman -S --needed python-gobject gtk4"
fi

echo "✅ Done."
echo "   • Launch: search 'Idus Elgato Light' in the menu, or run 'idus-elgato-light' in a terminal."
echo "   • First start offers to search your network for lights."
echo "   • OpenDeck: section 3 in the app lists a ready command (and key image) for every shortcut."
echo "   • New icon not showing? Log out/in (the icon cache is stubborn)."
