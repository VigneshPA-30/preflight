# Live view: watch Preflight work while an agent uses it

Two windows, both on `localhost`:

| Window | Shows | Start |
|---|---|---|
| **Preflight live** (port 8765) | Every tool call as it happens: sync, the clones being created and deleted, each plan's verdict and score, timings, the winner | `uv run python -m preflight.viewer` |
| **FalkorDB Browser** (port 3000) | The graphs themselves: the code graph, and (in demo mode) each plan's clone | `bash scripts/start_browser.sh` |

## 1. Give the MCP server a port (once)

The MCP server is started by Claude Desktop and owns the embedded database. Add two switches to its command in
`%APPDATA%\Claude\claude_desktop_config.json`, then **fully quit and reopen Claude Desktop**:

```json
{
  "mcpServers": {
    "preflight": {
      "command": "wsl.exe",
      "args": ["-d", "Ubuntu-24.04", "-e", "bash", "-lc",
        "cd ~/graphhacks/preflight && PREFLIGHT_DB=$HOME/.preflight/desktop.db PREFLIGHT_PORT=6390 PREFLIGHT_KEEP_UNIVERSES=1 uv run python -m preflight.server"]
    }
  }
}
```

- `PREFLIGHT_PORT=6390` opens the database on `127.0.0.1:6390` (this machine only) so the Browser can connect.
- `PREFLIGHT_KEEP_UNIVERSES=1` is for **demos**: the clones are not deleted after a simulation, so you can open them in the
  Browser. The next simulation (or a server restart) removes the old ones. Leave this out for normal use.
- Without these switches everything still works; you just get the activity feed and no graph pictures.

## 2. Start the two windows (in WSL, two terminals)

```bash
# terminal 1: activity feed
cd ~/graphhacks/preflight
PREFLIGHT_PORT=6390 uv run python -m preflight.viewer          # open http://localhost:8765

# terminal 2: FalkorDB Browser (first run downloads it, a few minutes)
cd ~/graphhacks/preflight
PREFLIGHT_PORT=6390 bash scripts/start_browser.sh              # open http://localhost:3000
```

The Browser needs Node.js. It asks for Node 24+; it also started on Node 22 in testing. To get Node 24:
`curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/master/install.sh | bash`, open a new terminal, `nvm install 24`.

## 3. How to read what you see

**The feed (port 8765)** is the trace. One card per tool call. In a `simulate_refactor_decisions` card, **click a plan's row**:
it opens what the plan changes, every call site that breaks (silent ones first), the tests that would stay green,
and ready-made queries for the Browser. The winner and any plan with silent breaks open by themselves.

**The Browser (port 3000)** shows the same facts as a picture. With `PREFLIGHT_KEEP_UNIVERSES=1` each plan leaves a graph behind:

| Graph in the list | What it is |
|---|---|
| `canon_<project>_...` | Your real code graph. Never changed by a simulation. |
| `uni_<run>_<n>_<plan name>` | A **clone** of it with that one plan applied. One per plan. |

Look at a clone like this:

1. In the Browser's graph list, select the clone (for example `uni_..._required_settings`).
2. In the viewer, open that plan and press **Copy** on a query. Paste it into the Browser's query box and run it.
3. The clone's nodes are colored by label:

| Label | Meaning |
|---|---|
| `Changed` | The function this plan modifies. Its properties show `old_params` and the new `params`. |
| `CaughtBreak` | A caller that breaks. A real test will fail, so you will see it. |
| `MaskedBreak` | A caller that breaks while every test that reaches it mocks the call. **Tests stay green.** |
| `UntestedBreak` | A caller that breaks and no test reaches it. |
| `LyingTest` | A test that mocks a broken call and stays green. |
| `ExpectedToFail` | A test the tool expects to fail. |

The four queries (all are in the viewer with a Copy button):

```cypher
MATCH (f:Changed) RETURN f                                                         -- what did this plan change?
MATCH (c)-[k:CALLS]->(f:Changed) WHERE k.broken = true RETURN c,k,f LIMIT 100      -- what breaks (picture)
MATCH (c)-[k:CALLS]->(f:Changed) WHERE k.status IN ['masked','untested']
OPTIONAL MATCH (t:LyingTest)-[m:MOCKS]->(f) RETURN c,k,f,t,m LIMIT 100            -- silent breaks and the mocks hiding them
MATCH p=(t:ExpectedToFail)-[:CALLS|REFERENCES*1..3]->(c)-[:CALLS {broken:true}]->(f:Changed) RETURN p LIMIT 25   -- how a failing test reaches a broken call
```

To prove the real graph is untouched, select `canon_...` and run `MATCH (f:Changed) RETURN f`. It returns nothing.

Each broken call is also marked on its `CALLS` edge: click an edge in the picture to read `status` and `reason`.

## 4. A good demo order

1. Send Claude one message first (for example "run check_graph_engine"): the database port only opens after the first tool call.
2. Ask the real question. Watch the feed: clones appear, verdicts arrive.
3. Open the risky plan in the feed and read the silent break.
4. In the Browser, run the third query on that plan's clone: you see the broken caller, the function it calls, and the test whose mock hides it.

## If the Browser says "Could not connect with the preconfigured connection"

The Browser cannot reach the database port. Check, in WSL:

```bash
ss -ltn | grep 6390                     # is anything listening? (no line = no port)
ps -eo pid,args | grep "[r]edis-server" # which database servers are running?
```

Then check the MCP server log (Windows: `%APPDATA%\Claude\logs\mcp-server-preflight.log`). It must contain
`opening FalkorDB at ... (TCP port 6390)`. If that text is missing, the config change did not reach the server:
check the line in `claude_desktop_config.json` and fully quit and reopen Claude Desktop.

Claude Desktop **kills** the MCP server when it quits, which can leave the old database server running without a port.
The server now repairs this itself on start (it restarts the old server with the port; the data is kept). If you still have an
older version, quit Claude Desktop, run `pkill redis-server` in WSL, and reopen Claude Desktop.

## Notes

- **Nothing here can break the MCP server.** The viewer only reads the event log and, through the TCP port, lists graphs.
  It never opens the database file, so closing it is safe. Event writing swallows every error.
- The Browser signs itself in (`FALKORDB_AUTO_CONNECT`) and the database port is bound to `127.0.0.1`. Do not expose either to a network.
- If `PREFLIGHT_PORT` is taken, the server logs it and continues without a port.
- The database port opens when Claude Desktop first uses the MCP server (your first tool call), not when the app starts. Send one tool call before starting the Browser.
- Windows reaches WSL servers on `localhost` (WSL2 default). If a page does not open, run `wsl --shutdown` and reopen.
- What was tested: the viewer page (loaded in a simulated browser and fed a real recorded session), the viewer's HTTP and live-stream
  endpoints, and the Browser run from source against an embedded database with a port (automatic sign-in worked and listed the graph).
  I could not watch the Browser *draw* graphs in a real browser window in my test environment, so check that once on your laptop.
