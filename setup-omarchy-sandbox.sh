#!/usr/bin/env bash
# Run this on the HOST (not inside a sandbox) to prepare the Docker Sandbox
# for Omarchy work: checks prerequisites, places bridge.py outside any mounted
# folder, registers it as an MCP server for this repo and sandbox, and prints
# the sbx run command.
#
# usage: ./setup-omarchy-sandbox.sh [sandbox-name]   (default: omarchy)
set -euo pipefail

SANDBOX_NAME="${1:-omarchy}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BRIDGE_DIR="$HOME/.local/share/omarchy-sbx-bridge"
BRIDGE_PY="$BRIDGE_DIR/bridge.py"

say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
fail() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

say "1. Checking prerequisites"

command -v sbx >/dev/null 2>&1 || fail "sbx is not installed. Run: yay -S docker-sbx-bin   (then: sudo usermod -aG kvm \$USER, log out and back in)"
[ -d /usr/share/omarchy ] || fail "/usr/share/omarchy not found — this does not look like an Omarchy system."
[ -d "$HOME/.config/omarchy" ] || fail "~/.config/omarchy not found."
[ -d "$HOME/.config/hypr" ] || fail "~/.config/hypr not found."
command -v python3 >/dev/null 2>&1 || fail "python3 is required to run the bridge."
# the same names the bridge accepts for --sandbox-name
[[ "$SANDBOX_NAME" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]] || fail "Invalid sandbox name: $SANDBOX_NAME"
# sbx mcp add splits --args on commas
case "$REPO_DIR$BRIDGE_PY" in
  *,*|*\"*|*$'\n'*) fail "The paths of this repo and of your home folder must not contain commas, quotes or line breaks." ;;
esac
echo "sbx, python3 and the Omarchy config folders are present."
echo "Run 'sbx diagnose' yourself if you have not already — it must report Virtualization OK."

# The bridge needs to know this repo (apps/ for launch_app) and the sandbox
# (for web_preview); both are fixed when it is registered.
BRIDGE_ARGS=("--workspace=$REPO_DIR" "--sandbox-name=$SANDBOX_NAME")

say "2. Placing bridge.py outside any sandbox-mounted folder"

mkdir -p "$BRIDGE_DIR"
if [ -f "$BRIDGE_PY" ]; then
  echo "Bridge already present at $BRIDGE_PY — leaving it as is."
  echo "(Delete it yourself first if you want this run to update it from $REPO_DIR/bridge.py.)"
else
  cp "$REPO_DIR/bridge.py" "$BRIDGE_PY"
  echo "Copied bridge.py to $BRIDGE_PY."
fi

python3 "$BRIDGE_PY" --check "${BRIDGE_ARGS[@]}" || fail "bridge.py --check failed; see the output above."

say "3. Registering the bridge as an MCP server"

err="$(mktemp)"
trap 'rm -f "$err"' EXIT
if sbx mcp add omarchy-bridge --command /usr/bin/python3 \
     --args="$BRIDGE_PY" --args="${BRIDGE_ARGS[0]}" --args="${BRIDGE_ARGS[1]}" 2>"$err"; then
  echo "Registered omarchy-bridge for $REPO_DIR and the sandbox $SANDBOX_NAME."
elif grep -qiE 'already|exists' "$err"; then
  echo "omarchy-bridge is already registered — leaving it as is."
  echo "If this repo moved or you chose another sandbox name, run 'sbx mcp rm omarchy-bridge' and this script again."
else
  cat "$err" >&2
  fail "Could not register omarchy-bridge with sbx mcp add."
fi

say "4. Ready"

cat <<EOF
Start the sandbox from this repo:

    cd "$REPO_DIR"
    sbx run --name $SANDBOX_NAME --static-mcp omarchy-bridge claude . ~/.config/omarchy ~/.config/hypr /usr/share/omarchy:ro

On first launch:
  - Network prompt: choose "Balanced".
  - Inside Claude, run /login.
  - Then run /mcp — only mcp-gateway should be listed. Disable any claude.ai
    connectors you see there.

To come back to this sandbox later:

    cd "$REPO_DIR" && sbx run --name $SANDBOX_NAME

To remove everything again:

    sbx rm $SANDBOX_NAME && sbx mcp rm omarchy-bridge
    rm -r ~/.local/share/omarchy-sbx-bridge ~/.local/state/omarchy-sbx-bridge ~/.cache/omarchy-sbx-bridge
EOF
