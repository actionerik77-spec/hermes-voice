#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
#  Hermes Voice — guided setup
#  Push-to-talk voice for your Hermes agent: press \ , talk, hear the reply.
#  Safe to re-run any time; existing settings are kept unless you change them.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
ENV_DIR="$HOME/.hermes/voicepill"
ENV_FILE="$ENV_DIR/voicepill.env"
HERMES_ENV="$HOME/.hermes/.env"

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m⚠\033[0m %s\n' "$*"; }

bold ""
bold "  HERMES VOICE — setup"
echo "  ------------------------------------------------------------"
echo "  What you get: a tiny menu-bar pill. Press \\ anywhere, talk,"
echo "  press \\ again — your words go to Hermes, the reply streams"
echo "  back and is spoken aloud. Press \\ mid-reply to barge in."
echo ""

# ── 1. prerequisites ─────────────────────────────────────────────────────────
bold "  [1/5] Checking prerequisites"
[ "$(uname)" = "Darwin" ] || { warn "macOS only."; exit 1; }
command -v swift >/dev/null 2>&1 || { warn "Xcode command-line tools missing — run: xcode-select --install"; exit 1; }
ok "swift toolchain"
PY="$HOME/.hermes/hermes-agent/venv/bin/python"
if [ ! -x "$PY" ]; then
  warn "Hermes Agent not found at ~/.hermes/hermes-agent"
  echo "     Install Hermes first: https://github.com/NousResearch/Hermes-Agent"
  exit 1
fi
ok "Hermes Agent at ~/.hermes/hermes-agent"

# ── 2. Hermes dashboard (the websocket this client talks to) ────────────────
bold "  [2/5] Hermes gateway / dashboard"
echo "     Hermes Voice talks to your Hermes dashboard websocket."
echo "     Local Hermes on this Mac → the default is right."
echo "     Hermes on another machine → use its address, e.g. http://100.x.y.z:9119"
echo "     (see server/ in this folder for the Tailscale proxy + watchdog kit)."
CUR_DASH=$(grep -s "^HERMES_DASHBOARD_URL=" "$ENV_FILE" 2>/dev/null | cut -d= -f2- || true)
printf "     Dashboard URL [%s]: " "${CUR_DASH:-http://127.0.0.1:9119}"
read -r DASH || DASH=""
DASH="${DASH:-${CUR_DASH:-http://127.0.0.1:9119}}"
if curl -s -m 5 -o /dev/null "$DASH/"; then
  ok "dashboard reachable at $DASH"
else
  warn "dashboard not reachable right now — continuing anyway."
  echo "     Start your Hermes gateway (hermes gateway start) and re-check later"
  echo "     with: airmic status"
fi

# ── 3. ElevenLabs API key (voice output) ────────────────────────────────────
bold "  [3/5] ElevenLabs voice (TTS)"
if grep -qs "^ELEVENLABS_API_KEY=" "$HERMES_ENV" 2>/dev/null; then
  ok "API key already present in ~/.hermes/.env"
else
  echo "     Get a key at https://elevenlabs.io (Profile → API keys)."
  printf "     ElevenLabs API key (input hidden; Enter to skip): "
  EL_KEY=""
  read -rs EL_KEY || EL_KEY=""
  echo ""
  if [ -n "$EL_KEY" ]; then
    mkdir -p "$(dirname "$HERMES_ENV")"
    printf 'ELEVENLABS_API_KEY=%s\n' "$EL_KEY" >> "$HERMES_ENV"
    chmod 600 "$HERMES_ENV"
    ok "key saved to ~/.hermes/.env (never echoed anywhere)"
  else
    warn "skipped — replies will be text-only until ELEVENLABS_API_KEY is in ~/.hermes/.env"
  fi
fi
CUR_VOICE=$(grep -s "^AIRMIC_ELEVENLABS_VOICE_ID=" "$ENV_FILE" 2>/dev/null | cut -d= -f2- || true)
echo "     Pick a voice ID at https://elevenlabs.io/app/voice-library (optional —"
echo "     Enter keeps your Hermes config.yaml tts.elevenlabs voice)."
printf "     Voice ID [%s]: " "${CUR_VOICE:-from config.yaml}"
read -r VOICE_ID || VOICE_ID=""
VOICE_ID="${VOICE_ID:-$CUR_VOICE}"

# ── 4. Local speech-to-text ──────────────────────────────────────────────────
bold "  [4/5] Speech-to-text (Parakeet, runs fully on-device)"
STT_PROVIDER="fluidaudio-parakeet"
FCLI="$HOME/hermes-voice-deps/FluidAudio/.build/release/FluidAudioCLI"
if [ -x "$FCLI" ]; then
  ok "FluidAudioCLI found"
else
  printf "     FluidAudio not found. Clone + build it now (~2 min)? [Y/n]: "
  read -r BUILD_STT || BUILD_STT=""
  case "${BUILD_STT:-Y}" in
    n|N)
      STT_PROVIDER="hermes"
      warn "using your Hermes-configured STT instead (no live partial transcripts)"
      ;;
    *)
      mkdir -p "$HOME/hermes-voice-deps"
      [ -d "$HOME/hermes-voice-deps/FluidAudio" ] || git clone --depth 1 https://github.com/FluidInference/FluidAudio.git "$HOME/hermes-voice-deps/FluidAudio"
      (cd "$HOME/hermes-voice-deps/FluidAudio" && swift build -c release --product FluidAudioCLI)
      ok "FluidAudioCLI built (the Parakeet model downloads itself on first use)"
      ;;
  esac
fi

# ── 5. write config + install ────────────────────────────────────────────────
bold "  [5/5] Writing config and installing"
mkdir -p "$ENV_DIR"
{
  echo "# Hermes Voice config — written by setup.sh (safe to edit by hand)"
  echo "HERMES_DASHBOARD_URL=$DASH"
  echo "AIRMIC_TTS_PROVIDER=elevenlabs"
  [ -n "${VOICE_ID:-}" ] && echo "AIRMIC_ELEVENLABS_VOICE_ID=$VOICE_ID"
  echo "AIRMIC_STT_PROVIDER=$STT_PROVIDER"
} > "$ENV_FILE"
ok "config → $ENV_FILE"

"$ROOT/install.sh"

echo ""
bold "  Setup complete."
echo "  ------------------------------------------------------------"
echo "  Launch:      open ~/Applications/HermesVoice.app"
echo "               (grant microphone access when macOS asks)"
echo "  Talk:        press \\  → speak → press \\  again"
echo "  Barge in:    press \\  while Hermes is talking"
echo "  End a call:  say \"hang up\", or use the ◭ menu-bar menu"
echo "  Mute voice:  ◭ menu → Voice Replies (TTS)"
echo "  Health:      airmic status"
echo ""
