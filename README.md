# omarchy-sbx-setup

Let an AI coding agent (Claude Code or another CLI agent) tweak your Omarchy
desktop and build plugins and apps for it, from inside a [Docker
Sandbox](https://docs.docker.com/ai/sandboxes/) (`sbx`): an isolated microVM
instead of your machine. The agent only ever sees the folders listed below,
plus a narrow host bridge for the handful of actions (restart the shell, read
logs, take a screenshot, launch a test app) that need to reach outside the VM.

This repository doubles as the sandbox's own workspace: clone it, run the
setup script once, and `sbx run` straight from it.

## What you can do with it

Ask the agent in plain words. Three kinds of work fit this setup:

- **Tweak your desktop.** "Make the window gaps smaller and turn off the blur",
  "Bind Super+Shift+B to the browser", "Move the clock to the left of the bar".
  The agent edits `~/.config/hypr` or `~/.config/omarchy`, backs up the
  Hyprland config and `shell.json` before changing them (as `CLAUDE.md` asks
  it to), reloads Hyprland or restarts the shell through the bridge, and reads
  back config errors and the shell log.
- **Build plugins.** "Build a bar widget that shows the CPU temperature", "Add a
  button to my plugin's panel". The agent works in `~/.config/omarchy/plugins/`,
  lints its QML with your system's `qmllint`, validates the manifest, restarts
  the shell, checks the log for QML errors, and takes a screenshot of the bar
  or the open panel before it asks you to try it.
- **Build apps.** "Write a small GTK app that ...". Each app lives in
  `apps/<name>/` of this repo. The bridge starts it on your desktop, isolated
  with bubblewrap, and moves its window to a workspace of its own (the stage);
  the agent reads its output and takes screenshots of its window. Web apps run
  inside the sandbox and open in a separate Chromium profile.

What stays with you: `sudo`, installing packages, files under `/etc`, systemd
units and the settings of other apps. The agent tells you the exact command to
run on the host.

## What gets mounted, and why

| Path | Access | Why |
|---|---|---|
| this repo | read-write | your app projects live under `apps/`, notes wherever you like |
| `~/.config/omarchy` | read-write | plugins, `shell.json`, themes, hooks — what you're editing |
| `~/.config/hypr` | read-write | Hyprland config |
| `/usr/share/omarchy` | **read-only** | Omarchy's own source, shell QML, skills |

Nothing else on your machine is reachable from inside the sandbox — not your
home directory, not `~/.ssh`, not `~/.local/state`, not other projects. The
sandbox also has no direct D-Bus, no audio, and talks to the host only
through `omarchy-bridge`. See `CLAUDE.md` for how its tools map to normal
Omarchy commands and how the agent uses them.

## What the bridge allows

`omarchy-bridge` is a small MCP server (`bridge.py`, Python standard library
only) that runs on the host with your permissions. It offers a fixed list of
tools and cannot run anything else. Every tool runs a fixed command, nothing
goes through a shell, and every argument is checked. Read it: it is the
entire trust boundary.

| Area | What the agent can do | Limits |
|---|---|---|
| Shell | restart it, list or rescan plugins, call a plugin's IPC, read the shell log | IPC only for plugins in `~/.config/omarchy/plugins` |
| Checks | `qmllint`, plugin validation, Hyprland reload and read-only queries | lint and validation run on copies made without following symlinks |
| Screenshots | the bar, or the whole screen while the `sbx` workspace (the stage) is shown | the stage may hold only the shell and apps the bridge started; nothing is captured while the screen is locked or a notification, the clipboard, polkit or the image picker is open; whole-screen and region captures only with `--allow-fullscreen` |
| Apps | start `apps/<name>/sbx-run.sh`, read its output, stop it | bubblewrap with its project folder, `/usr`, `/etc`, the GPU, the display, and your theme and fonts read-only; an empty home, no D-Bus, no audio, no network unless `--app-network` |
| Web preview | publish a sandbox port on `127.0.0.1` and open it | separate Chromium profile |
| Host check | whether packages or commands exist | read-only |

A stage screenshot switches your view to the stage for a moment and back.
Every call is logged to `~/.local/state/omarchy-sbx-bridge/bridge.log`.

## Prerequisites

- An Omarchy install (`/usr/share/omarchy`, `~/.config/omarchy`, `~/.config/hypr`).
- [Docker Sandboxes](https://docs.docker.com/ai/sandboxes/) (`sbx`):
  `yay -S docker-sbx-bin`, then `sudo usermod -aG kvm $USER` and log out/in.
- `sbx diagnose` reports virtualization OK, and `sbx login` is done.

Tested on a MacBook Air M2 with Omarchy 4.0.4 (Asahi, aarch64), `sbx` 0.47.0
and Claude Code.

## Quick start

```bash
git clone https://github.com/arikisonfire/omarchy-sbx-setup.git omarchy-sbx
cd omarchy-sbx
./setup-omarchy-sandbox.sh
```

This checks the prerequisites above, places `bridge.py` at
`~/.local/share/omarchy-sbx-bridge/bridge.py` (outside any folder a sandbox
ever mounts — see [Why bridge.py lives outside the repo at
runtime](#why-bridgepy-lives-outside-the-repo-at-runtime)), registers it with
`sbx mcp add` for this repo and the sandbox `omarchy`, and prints the exact
`sbx run` command to start the sandbox. Pass another name as the first
argument if you want one: `./setup-omarchy-sandbox.sh my-sandbox`.

Run the printed command, then inside the agent:

1. On the first network prompt, choose **Balanced**.
2. Run `/login` (Claude Code) or your agent's equivalent.
3. If using Claude Code, run `/mcp` — only `mcp-gateway` should be listed.
   Disable any other connectors shown there; they are not part of this setup.

You're now in a sandboxed session scoped to exactly the folders above. Try it:
ask the agent to launch the included `apps/hello` example.

## Coming back later

```bash
cd omarchy-sbx && sbx run --name omarchy
```

Reattaches to the same sandbox instead of creating a new one.

[sbxEnclave](https://github.com/arikisonfire/omarchy-sbx-enclave) shows your
sandboxes in the Omarchy bar, opens the agent again with one click, and lets
you answer the agent's network requests without a terminal.

## Removing everything

```bash
sbx rm omarchy && sbx mcp rm omarchy-bridge
rm -rf ~/.local/share/omarchy-sbx-bridge ~/.local/state/omarchy-sbx-bridge ~/.cache/omarchy-sbx-bridge
```

The first line removes the sandbox VM and the MCP registration, the second the
bridge, its log and the Chromium profile of the web preview. Neither touches
`~/.config/omarchy`, `~/.config/hypr` or this repo.

## Why bridge.py lives outside the repo at runtime

This repo is mounted **read-write** into the sandbox, so the agent running
inside it can edit anything here, including this copy of `bridge.py`.
`bridge.py` is what grants the sandbox session its (narrowly scoped) access
to the host; if the copy it actually runs sat inside a mounted folder, a
sandboxed agent could rewrite the very script that will next run with host
access, defeating the sandbox entirely. So the setup script copies it to
`~/.local/share/omarchy-sbx-bridge/bridge.py` — outside every mounted folder
— once, before the sandbox first starts, and the running MCP server always
reads from there, never from the repo.

If you update `bridge.py` in this repo later, review the diff, then delete
`~/.local/share/omarchy-sbx-bridge/bridge.py` and re-run the setup script to
pick up the change — don't automate that step from inside a sandbox session.

## Customizing

- `CLAUDE.md` is the agent's instructions for this environment — adjust it
  to your workflow (preferred language, how you organize `apps/`, your own
  conventions). It's deliberately generic here.
- `apps/hello/` is a minimal GTK4/libadwaita example showing the
  `sbx-run.sh` convention `launch_app` expects; replace or extend it.
- Bridge options: `--allow-fullscreen` (whole-screen and region screenshots),
  `--no-screenshots`, and `--app-network` (network for apps it starts). Add
  them when you register the bridge, after `sbx mcp rm omarchy-bridge`:

  ```bash
  sbx mcp add omarchy-bridge --command /usr/bin/python3 \
    --args="$HOME/.local/share/omarchy-sbx-bridge/bridge.py" \
    --args="--workspace=$PWD" --args=--sandbox-name=omarchy --args=--app-network
  ```

  Run it from this repo. The bridge picks up the change the next time the
  sandbox starts.

## Troubleshooting

- `sbx diagnose`
- `python3 ~/.local/share/omarchy-sbx-bridge/bridge.py --check --workspace="$PWD"` (from this repo)
- `tail ~/.local/state/omarchy-sbx-bridge/bridge.log`
- After changing `bridge.py` or its options: `sbx stop omarchy`, then start it again.

## License

[MIT](LICENSE)
