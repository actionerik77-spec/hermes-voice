#!/usr/bin/env bash
# Hermes Voice installer: CLI helpers to ~/.local, menu-bar app to ~/Applications.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"

if [ ! -x "$HOME/.hermes/hermes-agent/venv/bin/python" ]; then
  echo "Hermes Agent not found at ~/.hermes/hermes-agent (venv missing)." >&2
  echo "Install Hermes first: https://github.com/NousResearch/Hermes-Agent" >&2
  exit 1
fi

mkdir -p "$HOME/.local/bin" "$HOME/.local/share/airmic"
cp "$ROOT/cli/airmic.py" "$HOME/.local/share/airmic/airmic.py"
cp "$ROOT/cli/airmic" "$HOME/.local/bin/airmic"
cp "$ROOT/cli/parakeet-stt" "$HOME/.local/bin/parakeet-stt"
chmod +x "$HOME/.local/bin/airmic" "$HOME/.local/bin/parakeet-stt" "$HOME/.local/share/airmic/airmic.py"

"$ROOT/app/scripts/package.sh"

echo
echo "Installed. Launch with:  open ~/Applications/HermesVoice.app"
echo "Then press \\ to talk to your Hermes."
