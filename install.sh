#!/usr/bin/env bash
# Install glimpse (glimpse-jev): curl -fsSL https://raw.githubusercontent.com/Viverun/glimpse-jev/main/install.sh | bash
set -euo pipefail

REPO="${JEV_REPO:-https://github.com/Viverun/glimpse-jev.git}"
DIR="${JEV_HOME:-$HOME/.local/share/glimpse-jev}"
BIN="$HOME/.local/bin"

say() { printf '\033[1;36m==>\033[0m %s\n' "$1"; }

[ "$(uname -s)" = "Linux" ] || { echo "glimpse runs on Linux (GNOME on Wayland)."; exit 1; }
command -v git >/dev/null || { echo "Install git first."; exit 1; }

if ! command -v uv >/dev/null; then
  say "Installing uv (Python package manager)"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi

if [ -d "$DIR/.git" ]; then
  say "Updating $DIR"
  git -C "$DIR" pull --ff-only
else
  say "Downloading glimpse to $DIR"
  git clone --depth 1 "$REPO" "$DIR"
fi

say "Installing Python dependencies"
uv sync --project "$DIR" --quiet

mkdir -p "$BIN"
cat > "$BIN/glimpse" <<LAUNCH
#!/usr/bin/env bash
exec uv run --quiet --project "$DIR" python "$DIR/live/jev_cli.py" "\$@"
LAUNCH
chmod +x "$BIN/glimpse"
say "Installed the glimpse command in $BIN"

case ":$PATH:" in *":$BIN:"*) ;; *) echo "Add $BIN to your PATH (for example in ~/.bashrc or ~/.zshrc)." ;; esac

missing=""
for c in computer-use-linux ydotool gtk-launch; do command -v "$c" >/dev/null || missing="$missing $c"; done
[ -z "$missing" ] || echo "Also install:$missing  (computer-use-linux: npm install -g @agent-sh/computer-use-linux; see the README)."

say "Next: run  glimpse setup  to connect your planner endpoint and your TypeSafe (Jev) key."
