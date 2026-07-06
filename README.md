# Hermes Voice

A push-to-talk voice pill for [Hermes Agent](https://github.com/NousResearch/Hermes-Agent) on macOS.

Press `\` anywhere → talk → press `\` again. Your speech is transcribed **locally** (Parakeet CoreML), sent to your running Hermes, and the reply streams into a floating pill and is **spoken aloud** (ElevenLabs, or any TTS provider your Hermes config supports). Speech starts within a couple of seconds of the reply — sentence chunks synthesize while Hermes is still writing.

```
HermesVoice.app (menu bar, Swift)          your Hermes agent
   \  hotkey · mic · pill UI                     ▲
   └──► airmic (Python CLI) ──► Hermes dashboard WS (prompt.submit / NDJSON events)
             │
             ├──► Parakeet STT (local CoreML, ~0.4 s)
             └──► TTS via Hermes tts_tool (ElevenLabs et al.) → speakers
```

## Features

- **Global `\` hotkey** — bare backslash, no modifiers, no Accessibility permission needed. A menu toggle releases the key when you need to type a literal `\`.
- **Live partial transcript** while you speak.
- **Streaming TTS** — the reply is spoken in sentence chunks as it arrives, with an optional local STT firewall that verifies each chunk actually says what the text says.
- **Barge-in** — press `\` while Hermes is talking: playback stops instantly, the turn is interrupted server-side, and the mic re-opens.
- **Active-call model** — turns reuse one dedicated "Hermes Voice" session. Say *"hang up"* / *"new session"* (or use the menu) to start fresh.
- **Silence is not an error** — press `\` and say nothing: the pill just returns to idle.
- **Voice Replies (TTS) toggle** in the menu-bar menu (◭) — turn the voice off and get text-only turns; persists across restarts.
- **Tool ticker** — live view of the tools Hermes is running mid-turn, plus a work clock.

## Requirements

- macOS 13+, Xcode command-line tools (`swift`).
- **Hermes Agent** installed at `~/.hermes/hermes-agent` (with its venv) and its **dashboard running** (the websocket airmic talks to).
- **TTS**: an ElevenLabs key in `~/.hermes/.env` (`ELEVENLABS_API_KEY=...`) and a voice configured under `tts.elevenlabs` in `~/.hermes/config.yaml` (`voice_id`, optional `model_id`) — or any other provider your Hermes `tts` config supports (see env vars below).
- **STT**: [FluidAudio](https://github.com/FluidInference/FluidAudio) CLI for local Parakeet transcription:

  ```sh
  mkdir -p ~/hermes-voice-deps && cd ~/hermes-voice-deps
  git clone https://github.com/FluidInference/FluidAudio.git
  cd FluidAudio && swift build -c release --product FluidAudioCLI
  ```

  The Parakeet model downloads automatically on first use. Alternatively set `AIRMIC_STT_PROVIDER=hermes` to use whatever STT your Hermes config provides (live partial transcripts are Parakeet-only).

## Quick start

```sh
./setup.sh
```

The guided setup checks prerequisites, asks for your Hermes dashboard URL and ElevenLabs API key (stored in `~/.hermes/.env`, input hidden), optionally builds local Parakeet STT, writes the config, and installs everything. Then:

```sh
open ~/Applications/HermesVoice.app
```

Grant microphone access when prompted, and press `\`.

Configuration lives in `~/.hermes/voicepill/voicepill.env` (plain `KEY=VALUE`, safe to edit by hand, re-run `./setup.sh` any time). Real environment variables always override it.

## Manual install

```sh
./install.sh    # just copies the CLI + builds the app; no questions asked
```

If your Hermes dashboard is not on `http://127.0.0.1:9119`, set `HERMES_DASHBOARD_URL` in `~/.hermes/voicepill/voicepill.env` or the environment.

## CLI

The app is a thin shell around the `airmic` CLI, which you can use directly:

```sh
airmic chat --text "hello" --speak     # one voice turn
airmic chat --audio clip.wav --speak   # from a recording
airmic interrupt                       # interrupt the current turn
airmic hangup                          # end the call; next turn starts a new session
airmic speak --text "testing"          # TTS only
airmic status                          # connectivity / config report
```

`airmic chat` emits one JSON event per line (`transcript`, `delta`, `complete`, `tool`, `speaking`, `spoken`, `empty`, `error`, ...) — easy to build other clients on.

## Configuration (env vars)

| Variable | Default | Meaning |
|---|---|---|
| `HERMES_DASHBOARD_URL` | `http://127.0.0.1:9119` | Hermes dashboard websocket |
| `AIRMIC_TTS_PROVIDER` | `elevenlabs` | any provider in your Hermes `tts` config |
| `AIRMIC_ELEVENLABS_VOICE_ID` | from `config.yaml` | override ElevenLabs voice |
| `AIRMIC_ELEVENLABS_MODEL_ID` | from `config.yaml` | override ElevenLabs model |
| `AIRMIC_TTS_CHUNK_CHARS` | `420` | streaming TTS chunk size |
| `AIRMIC_TTS_MAX_CHARS` | `0` (unlimited) | cap spoken reply length |
| `AIRMIC_TTS_VERIFY` | `1` | STT-verify each spoken chunk |
| `AIRMIC_STT_PROVIDER` | `fluidaudio-parakeet` | or `hermes` for Hermes-config STT |
| `AIRMIC_CHAT_SESSION_TTL` | `0` (never) | idle seconds before auto-new-session |
| `AIRMIC_PARAKEET_MODEL_DIR` | auto | pin a Parakeet CoreML model dir |
| `FLUIDAUDIO_CLI` | `~/hermes-voice-deps/FluidAudio/.build/release/FluidAudioCLI` | FluidAudioCLI path |

## Remote setup (agent on another machine)

If your Hermes runs on a different machine (e.g. a Mac mini) and you talk to it over [Tailscale](https://tailscale.com), the `server/` directory has a battle-tested kit for the agent machine:

- **`tailscale_tcp_forward.py`** — tiny asyncio TCP forwarder that exposes a loopback-only dashboard on your Tailscale IP, with a client-IP allowlist. Keeps the dashboard itself bound to `127.0.0.1`.
- **`dashboard-tailscale-proxy.plist.example`** — launchd service for the forwarder (`RunAtLoad` + `KeepAlive`).
- **`watchdog_dashboard.sh`** + **`watchdog-dashboard.plist.example`** — a 60-second watchdog that curls the backend directly *and* end-to-end through the proxy, kickstarting or reloading whichever link is down. A wedged backend gets 3 strikes before restart; the stateless proxy is healed immediately. Without this, a proxy that dies (or was never loaded) turns every voice turn into `urlopen timed out`.

Setup on the agent machine:

```sh
cp server/tailscale_tcp_forward.py ~/.hermes/scripts/
cp server/watchdog_dashboard.sh ~/.hermes/bin/ && chmod +x ~/.hermes/bin/watchdog_dashboard.sh
# edit the two .plist.example files (user, IPs, ports, labels), then:
cp server/dashboard-tailscale-proxy.plist.example ~/Library/LaunchAgents/ai.hermes.dashboard-tailscale-proxy.plist
cp server/watchdog-dashboard.plist.example ~/Library/LaunchAgents/ai.hermes.watchdog-dashboard.plist
launchctl load -w ~/Library/LaunchAgents/ai.hermes.dashboard-tailscale-proxy.plist
launchctl load -w ~/Library/LaunchAgents/ai.hermes.watchdog-dashboard.plist
```

Then on the client machine set `HERMES_DASHBOARD_URL=http://<agent-tailscale-ip>:9119`.

> Tip: `launchctl bootstrap` sometimes fails with an I/O error over ssh — use `launchctl load -w` as above.

## Optional: instant acknowledgments

Drop short WAV clips into `~/.hermes/voicepill/acks/` named `on-it.wav`, `copy.wav`, `executing-now.wav`, `implementing-now.wav`, `executing-violently.wav`. The pill plays a random one the moment your prompt lands, so the call feels alive while Hermes works. No clips → silently skipped.

## Troubleshooting

- **Pill logs**: `~/Library/Logs/HermesVoice/hermesvoice.log`. Per-turn NDJSON: `~/.hermes/voicepill/logs/chat.ndjson`.
- **Everything mute?** A dead process may be holding the playback lock: `lsof "$TMPDIR/airmic-tts-playback.lock"` and kill the holder.
- **`\` types a backslash instead of recording** — the hotkey toggle in the ◭ menu is off, or another app grabbed the key.
- **`airmic status`** reports dashboard reachability, session state, and the active TTS route.

## Uninstall

```sh
rm -rf ~/Applications/HermesVoice.app ~/.local/share/airmic \
       ~/.local/bin/airmic ~/.local/bin/parakeet-stt ~/.hermes/voicepill
```
