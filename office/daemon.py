"""Entry point. Runs the office until told to stop.

The point of this process is that it outlives your browser. Start it once (or
let systemd start it), then open and close the GUI whenever you like.
"""

import argparse
import asyncio
import logging
import os
import signal
import sys

from . import config, server
from .office import Office


def _setup_logging(verbose=False):
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    handlers = [logging.FileHandler(config.LOG_PATH), logging.StreamHandler(sys.stdout)]
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=handlers,
    )
    logging.getLogger("asyncio").setLevel(logging.WARNING)


async def _main(args):
    problems = config.validate()
    if problems:
        for problem in problems:
            print(f"config error: {problem}", file=sys.stderr)
        return 2

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
          f"staff={len(config.STAFF_IDS)}\n")

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
