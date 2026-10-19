#!/usr/bin/env bash
# Install glimpse and everything it needs:
#   curl -fsSL https://raw.githubusercontent.com/Viverun/glimpse-jev/main/install.sh | bash
# Set GLIMPSE_SKIP_SYSTEM=1 to install only glimpse itself (no sudo, no system packages).
set -euo pipefail

REPO="${JEV_REPO:-https://github.com/Viverun/glimpse-jev.git}"
DIR="${JEV_HOME:-$HOME/.local/share/glimpse-jev}"
BIN="$HOME/.local/bin"

say()  { printf '\033[1;37m◐\033[0m %s\n' "$1"; }
note() { printf '  \033[2m%s\033[0m\n' "$1"; }
ask()  { local a; printf '%s [Y/n] ' "$1"; read -r a </dev/tty || a=y; [[ -z "$a" || "$a" =~ ^[Yy] ]]; }

[ "$(uname -s)" = "Linux" ] || { echo "glimpse runs on Linux with GNOME on Wayland."; exit 1; }
case "${XDG_CURRENT_DESKTOP:-}:${XDG_SESSION_TYPE:-}" in
  *GNOME*:wayland) ;;
  *) note "This session is not GNOME on Wayland. glimpse installs, but it operates only GNOME on Wayland." ;;
esac

# --- 1. system packages ------------------------------------------------------------------------
if [ -z "${GLIMPSE_SKIP_SYSTEM:-}" ]; then
  if   command -v apt-get >/dev/null; then PM="apt";    PKGS="git curl ydotool libgtk-3-bin python3-gi gir1.2-gtk-3.0 nodejs npm"
  elif command -v dnf     >/dev/null; then PM="dnf";    PKGS="git curl ydotool gtk3 python3-gobject nodejs npm"
  elif command -v pacman  >/dev/null; then PM="pacman"; PKGS="git curl ydotool gtk3 python-gobject nodejs npm"
  elif command -v zypper  >/dev/null; then PM="zypper"; PKGS="git curl ydotool gtk3-tools python3-gobject-Gdk nodejs npm"
  else PM=""; fi

  if [ -n "$PM" ]; then
    say "glimpse needs these system packages: $PKGS"
    note "and: access to /dev/uinput for ydotool (a udev rule), and your user in the 'input' group (for the stop key)."
    if ask "Install them now with sudo?"; then
      case "$PM" in
        apt)    sudo apt-get update -qq && sudo apt-get install -y $PKGS ;;
        dnf)    sudo dnf install -y $PKGS ;;
        pacman) sudo pacman -S --needed --noconfirm $PKGS ;;
        zypper) sudo zypper --non-interactive install $PKGS ;;
      esac
      echo 'KERNEL=="uinput", GROUP="input", MODE="0660", OPTIONS+="static_node=uinput"' | sudo tee /etc/udev/rules.d/80-glimpse-uinput.rules >/dev/null
      sudo udevadm control --reload-rules && sudo udevadm trigger /dev/uinput 2>/dev/null || true
      if ! id -nG "$USER" | grep -qw input; then
        sudo usermod -aG input "$USER"
        NEED_RELOGIN=1
      fi
    else
      note "Skipped. Install them yourself later: $PKGS"
    fi
  else
    note "Unknown package manager. Install these yourself: git, curl, ydotool, gtk-launch, python3-gi (GTK 3), nodejs, npm."
  fi

  # --- 2. computer-use-linux (reads windows, sends clicks and keys) -------------------------------
  if command -v npm >/dev/null; then  # install, or upgrade an old version (glimpse needs 0.7.18 or newer)
    say "Installing the latest computer-use-linux"
    npm install -g --allow-scripts=@agent-sh/computer-use-linux @agent-sh/computer-use-linux@latest 2>/dev/null \
      || npm install -g @agent-sh/computer-use-linux@latest 2>/dev/null \
      || sudo npm install -g @agent-sh/computer-use-linux@latest
  fi

  # --- 3. the ydotool daemon, as a user service ------------------------------------------------
  if command -v ydotoold >/dev/null && command -v systemctl >/dev/null; then
    mkdir -p "$HOME/.config/systemd/user"
    if [ ! -f "$HOME/.config/systemd/user/ydotoold.service" ]; then
      cat > "$HOME/.config/systemd/user/ydotoold.service" <<UNIT
[Unit]
Description=ydotool daemon (input for glimpse)
After=graphical-session.target
PartOf=graphical-session.target

[Service]
ExecStart=$(command -v ydotoold)
Restart=on-failure
RestartSec=2

[Install]
WantedBy=default.target
UNIT
    fi
    systemctl --user daemon-reload
    systemctl --user enable --now ydotoold.service >/dev/null 2>&1 || note "ydotoold starts after you log in again."
  fi

  # --- 4. accessibility (apps and browsers publish their controls) ---------------------------------
  command -v gsettings >/dev/null && gsettings set org.gnome.desktop.interface toolkit-accessibility true 2>/dev/null || true
  mkdir -p "$HOME/.config/environment.d"
  printf '%s\n' '# glimpse: apps publish their controls to accessibility tools' \
    'GNOME_ACCESSIBILITY=1   # Firefox, Zen, LibreWolf' 'ACCESSIBILITY_ENABLED=1   # Chrome, Chromium, Brave, Edge' \
    > "$HOME/.config/environment.d/90-glimpse.conf"
  NEED_RELOGIN=1
fi

# --- 5. glimpse itself -------------------------------------------------------------------------
command -v git >/dev/null || { echo "Install git first."; exit 1; }
if ! command -v uv >/dev/null; then
  say "Installing uv (Python package manager)"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi

if [ -d "$DIR/.git" ]; then
  say "Updating glimpse in $DIR"
  git -C "$DIR" pull --ff-only --quiet
else
  say "Downloading glimpse to $DIR"
  git clone --depth 1 --quiet "$REPO" "$DIR"
fi

say "Installing Python dependencies"
uv sync --project "$DIR" --quiet

mkdir -p "$BIN"
cat > "$BIN/glimpse" <<LAUNCH
#!/usr/bin/env bash
exec uv run --quiet --project "$DIR" python "$DIR/live/jev_cli.py" "\$@"
LAUNCH
chmod +x "$BIN/glimpse"
VERSION="$("$BIN/glimpse" --version 2>/dev/null || echo glimpse)"
say "Installed $VERSION"

case ":$PATH:" in *":$BIN:"*) ;; *) note "Add $BIN to your PATH (in ~/.bashrc or ~/.zshrc)." ;; esac
missing=""
for c in computer-use-linux ydotool gtk-launch; do command -v "$c" >/dev/null || missing="$missing $c"; done
[ -z "$missing" ] || note "Still missing:$missing"
[ -z "${NEED_RELOGIN:-}" ] || note "Log out and log in again once: you were added to the 'input' group."

say "Next: run  glimpse setup  to connect your planner endpoint and your TypeSafe (Jev) key."
