"""Entry point. Runs the office until told to stop.

The point of this process is that it outlives your browser. Start it once (or
let systemd start it), then open and close the GUI whenever you like.
"""

import argparse
import asyncio
import logging
import logging.handlers
import os
import signal
import sys

from . import config, server
from .office import Office


def _setup_logging(verbose=False):
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    # Rotating, because a daemon that runs for months on a VPS otherwise
    # leaves a log the size of the disk. 10 MB x 5 is weeks of normal traffic.
    handlers = [
        logging.handlers.RotatingFileHandler(
            config.LOG_PATH, maxBytes=10 * 1024 * 1024, backupCount=5),
        logging.StreamHandler(sys.stdout),
    ]
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=handlers,
    )
    logging.getLogger("asyncio").setLevel(logging.WARNING)


def _isolate_claude_config():
    """Give the Agent SDK its own config directory so it never reads the
    human's ~/.claude - no personal settings.json allow-rules ahead of the
    approval gate, no personal plugins or skills in the context window.

    Only possible when the credential is in the environment. A `claude login`
    on this box stores it under ~/.claude, and hiding that would break auth."""
    if not config.ISOLATE_CLAUDE_CONFIG:
        return "off (OFFICE_ISOLATE_CLAUDE_CONFIG=0)"
    if not config._credential_in_env(os.environ):
        return ("off: no CLAUDE_CODE_OAUTH_TOKEN / ANTHROPIC_API_KEY in the environment, "
                "so the SDK must read ~/.claude for its login")
    config.CLAUDE_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    os.environ["CLAUDE_CONFIG_DIR"] = str(config.CLAUDE_CONFIG_DIR)
    return f"on ({config.CLAUDE_CONFIG_DIR})"


async def _main(args):
    problems = config.validate()
    if problems:
        for problem in problems:
            print(f"config error: {problem}", file=sys.stderr)
        return 2

    isolation = _isolate_claude_config()
    logging.getLogger("office").info("claude config isolation: %s", isolation)
    # Must run before the first SDK call and after config has read the env.
    withheld = config.scrub_process_environment()
    logging.getLogger("office").info(
        "withheld from agents (%d): %s", len(withheld), ", ".join(withheld) or "-")

    office = Office()
    await office.start()
    httpd = server.serve(office)
    config.PID_PATH.write_text(str(os.getpid()))

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with __import__("contextlib").suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    url = f"http://{config.HOST}:{config.PORT}"
    print(f"\n  Digital Office is open.  {url}")
    print(f"  backend={office.backend.name}  auth={office.backend.describe_auth()}")
    print(f"  daily budget=${config.DAILY_BUDGET_USD:.2f}  "
          f"token ceiling={config.SESSION_TOKEN_BUDGET or 'off'}  "
          f"concurrency={config.MAX_CONCURRENT}  tz={config.TZ or 'system'}")
    print(f"  claude config isolation: {isolation}")
    if config.DOTENV_KEYS:
        print(f"  .env: {config.DOTENV_KEYS} key(s) read; secrets kept out of the environment")
    print(f"  withheld from agents: {len(withheld)} variable(s) (names in the log)")
    print(f"  staff={len(config.STAFF_IDS)}\n")

    try:
        await stop.wait()
    finally:
        print("closing the office...")
        httpd.shutdown()
        await office.stop()
        config.PID_PATH.unlink(missing_ok=True)
    return 0


def main():
    parser = argparse.ArgumentParser(prog="digital-office")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    _setup_logging(args.verbose)
    try:
        sys.exit(asyncio.run(_main(args)))
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
