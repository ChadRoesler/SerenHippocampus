"""Entry point for `python -m seren_hippocampus` / the `seren-hippocampus` script."""
from __future__ import annotations

import argparse
import sys

import uvicorn
from seren_meninges.exposure import enforce_server

from .app import create_app
from .config import ENV_PREFIX, load_config


def _force_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass


def main() -> None:
    _force_utf8_stdio()
    parser = argparse.ArgumentParser(
        prog="seren_hippocampus",
        description="SerenHippocampus - the sleep cycle for SerenMemory.")
    parser.add_argument("--config", "-c", default=None,
                        help="Path to seren-hippocampus.yaml (default: $SEREN_HIPPOCAMPUS_CONFIG, "
                             "then ~/seren-hippocampus/seren-hippocampus.yaml, then defaults).")
    args = parser.parse_args()
    cfg = load_config(args.config)
    enforce_server(cfg.server, service="seren-hippocampus", env_prefix=ENV_PREFIX,
                   log=lambda m: print(m, file=sys.stderr))
    app = create_app(cfg)
    uvicorn.run(app, host=cfg.server.host, port=cfg.server.port, log_level="info")


if __name__ == "__main__":
    main()
