"""Command line entry point for edcb-bridge."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import os
import shutil
from argparse import ArgumentDefaultsHelpFormatter, ArgumentParser, Namespace, RawDescriptionHelpFormatter

from . import __version__
from .config import RECORDERS, Config
from .server import serve

DESCRIPTION = """\
EDCB (EpgTimerSrv) CtrlCmd compatible bridge to EPGStation / mirakc for KonomiTV.

Every option can also be given by the environment variable shown in brackets."""


class HelpFormatter(ArgumentDefaultsHelpFormatter, RawDescriptionHelpFormatter):
    """Show argument defaults while keeping the description's own line breaks."""


def parse_args(args: list[str] | None = None) -> Namespace:
    """Parse the command line.

    Args:
        args: Arguments to parse instead of `sys.argv[1:]`. Used by the tests.

    Returns:
        The parsed arguments. Their names match the fields of `Config`.
    """
    parser = ArgumentParser(
        prog="edcb-bridge",
        description=DESCRIPTION,
        formatter_class=lambda prog: HelpFormatter(
            prog,
            width=shutil.get_terminal_size(fallback=(120, 50)).columns,
            max_help_position=40,
        ),
    )
    env = Config.from_env()

    def add(name: str, help_text: str, **kwargs: object) -> None:
        default = getattr(env, name)
        parser.add_argument(
            f"--{name.replace('_', '-')}",
            default=default,
            type=type(default),
            help=f"{help_text} [{name.upper()}]",
            **kwargs,  # ty: ignore[invalid-argument-type]
        )

    add("listen_host", "address to listen on for CtrlCmd")
    add("listen_port", "port to listen on for CtrlCmd")
    add("mirakc_url", "mirakc (or Mirakurun) URL to read services and programs from")
    add("recorder", "recording backend", choices=RECORDERS)
    add("epgstation_url", "EPGStation URL (recorder=epgstation)")
    add("recorded_dir", "recording folder as seen by KonomiTV")
    add("mirakc_content_path_template", "recorded file name (recorder=mirakc)")
    add("start_margin", "start margin in seconds shown in KonomiTV (display only)")
    add("end_margin", "end margin in seconds shown in KonomiTV (display only)")
    add("epg_cache_ttl", "seconds to cache the program guide")
    add("http_timeout", "HTTP timeout in seconds")
    parser.add_argument(
        "--log-level",
        default=os.environ.get("LOG_LEVEL", "INFO").upper(),
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="log level [LOG_LEVEL]",
    )
    parser.add_argument("-V", "--version", action="version", version=f"%(prog)s {__version__}")
    return parser.parse_args(args)


def build_config(parsed: Namespace) -> Config:
    """Turn parsed arguments into a `Config`.

    Returns:
        The config.
    """
    return Config(**{field.name: getattr(parsed, field.name) for field in dataclasses.fields(Config)})


def main(args: list[str] | None = None) -> None:
    """Run the bridge until interrupted."""
    parsed = parse_args(args)
    logging.basicConfig(level=parsed.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # httpx のリクエストごとの INFO ログは冗長なので抑える
    logging.getLogger("httpx").setLevel(logging.WARNING)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(serve(build_config(parsed)))


if __name__ == "__main__":
    main()
