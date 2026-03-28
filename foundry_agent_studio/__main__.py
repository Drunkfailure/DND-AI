"""Run the HTTP server (same port as the Foundry bridge, default 17890)."""

from __future__ import annotations

import sys

import uvicorn

from foundry_agent_studio.app import create_app
from foundry_agent_studio.db import get_config, open_db
from foundry_agent_studio.paths import db_path


def main() -> None:
    conn = open_db(db_path())
    try:
        port_s = get_config(conn, "bridge_port") or "17890"
        port = int(port_s)
    finally:
        conn.close()

    app = create_app()
    print(f"[fas] Open http://127.0.0.1:{port}/ (bridge + UI on same port)", file=sys.stderr)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")


if __name__ == "__main__":
    main()
