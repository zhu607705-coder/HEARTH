"""Local demo and server entry points; secrets are never embedded in source code."""
from __future__ import annotations

import argparse
import json
import os
import secrets
from pathlib import Path

from .models import DemoRequest, Reading, load_settings
from .simulator import simulate


def main() -> None:
    parser = argparse.ArgumentParser(description="HEARTH freshness research demonstrator")
    parser.add_argument("--config", type=Path, help="validated JSON configuration")
    commands = parser.add_subparsers(dest="command", required=True)
    demo = commands.add_parser("demo")
    demo.add_argument("--scenario", choices=["normal", "cooled", "heatwave", "outage", "sensor_fault"], default="normal")
    demo.add_argument("--hours", type=float, default=24)
    demo.add_argument("--interval-minutes", type=int, default=10)
    demo.add_argument("--seed", type=int, default=20260916)
    demo.add_argument("--output", type=Path, default=Path("artifacts/demo.json"))
    server = commands.add_parser("serve")
    server.add_argument("--db", type=Path, default=Path("artifacts/edge.db"))
    server.add_argument("--credentials", type=Path, default=Path("artifacts/credentials.json"))
    server.add_argument("--port", type=int, default=8000)
    commands.add_parser("contracts")
    args = parser.parse_args()
    settings = load_settings(args.config)
    if args.command == "demo":
        request = DemoRequest(scenario=args.scenario, hours=args.hours,
                              interval_minutes=args.interval_minutes, seed=args.seed)
        report = simulate(request, settings)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
        print(f"SYNTHETIC ONLY | output: {args.output}")
        print(f"25 C / 30 C conditional quality-lifetime ratio: {report['cooling_lifetime_ratio_25_vs_30']:.4f}")
        print(f"Audit chain valid: {report['ledger']['valid']}; samples: {len(report['samples'])}")
    elif args.command == "serve":
        import uvicorn

        from .api import create_app
        if not 1 <= args.port <= 65535:
            parser.error("port must be 1..65535")
        args.db.parent.mkdir(parents=True, exist_ok=True)
        args.credentials.parent.mkdir(parents=True, exist_ok=True)
        if args.credentials.exists():
            if os.name == "posix" and args.credentials.stat().st_mode & 0o077:
                parser.error("credentials file must have mode 0600; run chmod 600 on it")
            keys = json.loads(args.credentials.read_text())
        else:
            keys = {role: secrets.token_urlsafe(32) for role in ("viewer", "ingest", "operator")}
            descriptor = os.open(args.credentials, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w") as file:
                json.dump(keys, file)
        app = create_app(args.db, settings, keys)
        print(f"Research-only dashboard: http://127.0.0.1:{args.port}")
        print(f"Role credentials stored locally at {args.credentials}; do not commit this file.")
        uvicorn.run(app, host="127.0.0.1", port=args.port, workers=1, limit_concurrency=32,
                    timeout_keep_alive=5, access_log=False)
    else:
        import tempfile

        from .api import create_app
        output = Path("contracts")
        output.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory() as temporary:
            keys = {role: secrets.token_urlsafe(32) for role in ("viewer", "ingest", "operator")}
            app = create_app(Path(temporary) / "edge.db", settings, keys, watchdog=False)
            for name, data in [("readings.schema.json", Reading.model_json_schema()),
                               ("openapi.json", app.openapi())]:
                (output / name).write_text(json.dumps(data, indent=2, allow_nan=False) + "\n")
        print("Generated contracts/readings.schema.json and contracts/openapi.json")


if __name__ == "__main__":
    main()
