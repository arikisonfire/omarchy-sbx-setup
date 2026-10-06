# Omarchy development in a Docker Sandbox

This is a template `CLAUDE.md` for an agent running in this sandbox. Adjust
it to your own preferences — e.g. add a line for which language to reply in,
or notes on how you organize `apps/`.

## Where you're running

- You're running in a Docker Sandbox (`sbx`), a microVM with its own kernel
  (Ubuntu), not on the host. `sudo` and `apt` only affect the VM.
- Only these folders are mounted from the host, at the same paths as on the
  host:
  - this repo: read-write. App/plugin projects live in `apps/<project>/`.
  - `~/.config/omarchy`: read-write. Contains `plugins/`, `shell.json`,
    `shell.toml`, `themes/`, `hooks/` and `extensions/`.
  - `~/.config/hypr`: read-write. Hyprland configuration.
  - `/usr/share/omarchy`: read-only. Omarchy's own sources, the shell QML
    (`shell/`), the default Hyprland config (`default/hypr/`) and skills.
- Nothing else on the host is visible to you. Don't try to reach it.

## Your changes take effect immediately on the host

- The Omarchy shell on the host reloads saved files under
  `~/.config/omarchy/plugins/` and `shell.json` right away. `~/.config/hypr`
  takes effect on the next Hyprland reload.
- So save only small, complete changes — not half-finished intermediate
  states.
- Before editing `~/.config/hypr/*.lua` or `~/.config/omarchy/shell.json`,
  make a backup `<file>.bak.<unix-time>`.
- Don't touch: `.git/hooks/`, local git config, or any hooks directory under
  `~/.config/omarchy/hooks/` unless the user explicitly asks for it.

## Host actions only go through the `omarchy-bridge` MCP server

These host commands don't exist inside the sandbox. Use the matching bridge
tool instead:

| Host command | Bridge tool |
|---|---|
| `omarchy restart shell`, `omarchy-restart-shell` | `restart_shell`. Returns the log lines since the restart. |
| `omarchy-shell shell rescanPlugins` / `listPlugins` | `rescan_plugins` / `list_plugins` |
| `omarchy-shell <plugin-id>[.<suffix>] <method> …` | `plugin_ipc` (`plugin_id`, `method`, optional `target_suffix` and `args`). Only for plugins under `~/.config/omarchy/plugins`, e.g. to open a panel for a screenshot. |
| `journalctl --user -t omarchy-shell` | `shell_logs` (`lines`, `filter`, `since_seconds`) |
| `omarchy plugin validate <folder>` | `validate_plugin` (`plugin_id`) |
| `qmllint` | `qmllint` (`path`). Runs with the host's Qt version and resolves `qs.*` imports. |
| `hyprctl reload` | `hypr_reload`. Returns `configerrors`. |
| `hyprctl -j monitors/clients/binds/getoption …` | `hypr_query` (`what`, `option`) |
| Start an app on the host, read its output, stop it | `launch_app`, `app_logs`, `stop_app` (see below) |
| View a web app | `web_preview` (see below) |
| Check what's installed on the host | `host_check` (`packages`, `commands`) |
| View the screen | `screenshot`, `stage_list`, `stage_window` (see below) |

If a tool fails, call `status` first.

## Screenshots and the stage (workspace `sbx`)

- `screenshot` with `target=bar`: just the bar strip. No workspace switch.
  For bar widgets.
- `screenshot` with `target=stage`:
  - The bridge briefly switches to the empty workspace `sbx` (the "stage")
    and captures the whole monitor: bar, shell panels and windows on the
    stage.
  - Then switches back.
  - For plugin panels. Open them first with `plugin_ipc` if the plugin has
    an IPC function for that.
- `screenshot` with `target=window` and `address`: like `stage`, but cropped
  to a development window. For apps.
- **Development windows** are only apps the bridge started, and windows of
  the Omarchy shell. Only they may be on the stage. `stage_list` shows them
  with their addresses, `stage_window` moves one there.
- If a foreign window is on the stage, or a sensitive overlay is open
  (notification, clipboard, polkit, lock screen), the bridge refuses the
  capture. That's intentional, not a bug.
- Every stage screenshot briefly switches the user's view. Take them
  purposefully, not in a loop.
- `target=screen` and `target=geometry` only work if the user enabled them.
  Don't ask for them just to work around that.

## Plugin development workflow

0. Read any notes you keep about the plugin first, if you keep any.
1. Change QML/JS in `~/.config/omarchy/plugins/<id>/`.
2. Run `qmllint` on the changed `.qml` files. If `manifest.json` changed,
   also run `validate_plugin`.
3. Add a unique, temporary `console.log` marker and call `restart_shell`.
   Auto-reload on save often keeps stale QML.
4. Check the logs `restart_shell` returns, or `shell_logs`: does the marker
   appear? Any QML errors?
5. For a visual check, call `screenshot`: `target=bar` for widgets,
   `target=stage` for panels.
6. Only then let the user test. Remove the marker afterwards.
7. If a plugin's own child process can crash under rapid repeated saves
   (e.g. anything that shells out on load), batch your edits through a
   scratch copy outside the plugin folder, lint there, then copy the
   finished files in — instead of saving the live plugin file repeatedly.

## Building apps

- Each app gets its own folder `apps/<project>/` with a start script
  `sbx-run.sh`, run with bash from the project folder.
- `launch_app` starts the app **on the host**, isolated with bubblewrap. Its
  windows land on the stage. Afterwards:
  - `screenshot` with `target=window` (address from the response or from
    `stage_list`)
  - `app_logs` for its output
  - `stop_app` to stop it
- For TUI apps, call `launch_app` with `terminal=true`. The script then runs
  in a `foot` window.
- **The app runs with the host's libraries, not the sandbox's.** Check with
  `host_check` what packages and commands exist on the host (e.g. `python`,
  `python-gobject`, `gtk4`, `libadwaita`, `qt6-declarative`, `quickshell`).
  - Whatever is missing, the user has to install on the host. Tell them
    which package and why.
  - Only build statically linked compiled programs for the host's
    architecture, built inside the sandbox — e.g. Go with
    `CGO_ENABLED=0` or Rust with the musl target. Dynamically linked
    binaries against the sandbox's glibc often won't run on the host.
- The app sees, on the host:
  - its project folder (read-write)
  - `/usr`, `/etc`, the GPU and the Wayland display
  - read-only: the desktop theme and fonts
- The app does **not** have, on the host:
  - a home directory — it's empty and ephemeral. Put data/settings in the
    project folder, e.g. `XDG_CONFIG_HOME="$PWD/.config"` in `sbx-run.sh`.
  - D-Bus — so no notifications, no tray, no portals
  - audio
  - network, unless the user enabled it
  - If an app needs one of these, explain what and why; the user decides on
    the host whether to allow it.
- **Omarchy look:**
  - libadwaita apps automatically follow the host's light/dark setting (the
    bridge sets `ADW_DEBUG_COLOR_SCHEME`).
  - Omarchy doesn't otherwise theme GTK apps. For theme colors, read
    `~/.local/state/omarchy/current/theme/colors.toml` at runtime (e.g.
    `accent`, `background`, `foreground`) — readable from inside the
    isolated app.
- **Web apps:** start the dev server in the sandbox on `0.0.0.0:<port>`, not
  `127.0.0.1`. Then call `web_preview` with `port`. The bridge publishes the
  port on the host and opens it in a separate Chromium profile on the stage.

## What only works on the host

Only the user — or an agent running directly on the host — can do these:
- `sudo`, a system package manager, files under `/etc`, kernel parameters,
  systemd units
- `~/.local/share` (icons, `.desktop` files)
- other apps' config under `~/.config`
- installing packages an app needs

Give exact commands for these.

## Git

- Push and open pull requests directly from inside the sandbox if your
  credentials are set up for it (see your sandbox provider's docs on
  credential injection). Otherwise commit and tell the user to push from the
  host.
- Set a dedicated git identity for new repos you create from here if you
  don't want your host identity used:
  `git config user.name "..."` / `git config user.email "..."`.
