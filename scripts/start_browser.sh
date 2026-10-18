#!/usr/bin/env bash
# Starts the official FalkorDB Browser (no Docker) and connects it to Preflight's embedded database.
#   PREFLIGHT_PORT=6390 bash scripts/start_browser.sh        # then open http://localhost:3000
# The MCP server must be started with the SAME PREFLIGHT_PORT (see docs/LIVE_VIEW.md).
set -euo pipefail

PORT="${PREFLIGHT_PORT:-6390}"
DIR="${FALKORDB_BROWSER_DIR:-$HOME/falkordb-browser}"

command -v node >/dev/null || { echo "Node.js is missing. Install Node 24+ (see docs/LIVE_VIEW.md)."; exit 1; }
MAJOR="$(node -p 'process.versions.node.split(".")[0]')"
if [ "$MAJOR" -lt 24 ]; then
  echo "Note: the Browser asks for Node 24+, you have $(node --version). It started fine on Node 22 in testing; if it fails, upgrade Node."
fi

if [ ! -d "$DIR" ]; then
  git clone --depth 1 https://github.com/FalkorDB/falkordb-browser.git "$DIR"
fi
cd "$DIR"
[ -d node_modules ] || npm install --no-audit --no-fund

# Always rewrite the connection settings (the port may have changed); keep secrets stable between runs.
SECRET_FILE=".preflight-secrets"
if [ ! -f "$SECRET_FILE" ]; then
  { echo "AUTH_SECRET=$(openssl rand -hex 32)"; echo "ENCRYPTION_KEY=$(openssl rand -hex 32)"; } > "$SECRET_FILE"
fi
{
  echo "AUTH_URL=http://localhost:3000/"
  echo "PORT=3000"
  echo "ALLOWED_ORIGINS=http://localhost:3000"
  cat "$SECRET_FILE"
  echo "FALKORDB_HOST=127.0.0.1"
  echo "FALKORDB_PORT=$PORT"
  echo "FALKORDB_AUTO_CONNECT=true"     # the Browser signs itself in; it only listens on localhost
} > .env.local

echo "FalkorDB Browser -> http://localhost:3000   (database on 127.0.0.1:$PORT)"
exec npm run dev
