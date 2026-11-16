#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Thin stdio entrypoint for the headless rhwp-backed hwp-mcp server."""

import logging
import os
import sys

current_dir = os.path.dirname(os.path.abspath(__file__))
if current_dir not in sys.path:
    sys.path.insert(0, current_dir)

logging.basicConfig(
    level=logging.INFO,
    filename=os.path.join(current_dir, "hwp_mcp_stdio_server.log"),
    filemode="a",
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
stderr_handler = logging.StreamHandler(sys.stderr)
stderr_handler.setFormatter(logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"))
root_logger = logging.getLogger("hwp-mcp")
root_logger.addHandler(stderr_handler)

from src.server import main  # noqa: E402

if __name__ == "__main__":
    main()
