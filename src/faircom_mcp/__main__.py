from __future__ import annotations

import argparse
import asyncio

from faircom_mcp.config import load_config
from faircom_mcp.logging_utils import configure_structured_logging
from faircom_mcp.server import create_http_app, create_stdio_server


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="FairCom MCP server")
    parser.add_argument(
        "--transport",
        choices=("http", "sse", "stdio"),
        default="stdio",
        help="Transport mode to run",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    configure_structured_logging()
    config = load_config()

    if args.transport == "stdio":
        server = create_stdio_server(config)
        asyncio.run(server.run_async(transport="stdio"))
        return 0

    app = create_http_app(config, transport=args.transport)
    host = config.transport.host
    port = config.transport.port

    import uvicorn

    uvicorn.run(app, host=host, port=port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
