#!/usr/bin/env python3
"""
Crawl one date with one crawler process per chain, all at the same time.

The data for a date stays in `<root>/data/<DATE>/` between runs. Each run
crawls only the chains whose data is missing or incomplete, and publishes
`<archive-dir>/<DATE>.zip` when the data changed. The crawlers do not change:
each chain runs as `python -m crawler.cli.crawl -c <chain>`.

Layout:

    <root>/data/<DATE>/<chain>/       published data, zipped as-is
    <root>/staging/<DATE>/<chain>/    output of the attempt in progress
    <root>/logs/<DATE>/<chain>.log    crawler output, appended per attempt

Peak memory per chain is reported on Linux and macOS only.
"""

import argparse
import json
import logging
import os
import secrets
import shutil
import signal
import subprocess
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from time import sleep, time
from typing import IO, NamedTuple
from zipfile import ZipFile

from crawler.crawl import get_chains
from crawler.store.output import copy_archive_info, create_archive

logger = logging.getLogger("crawler.daily")

# A chain is complete when each of its counts is at least this share of the
# expected count.
THRESHOLD = 0.9

# A crawler process that runs longer than this (in seconds) is killed and its
# attempt discarded, so one stuck chain does not hold back the ZIP of the other
# chains. The slowest chain, Boso, took 66 min on 2026-11-24.
CHAIN_TIMEOUT = 3 * 3600

# (stores, products, prices) from a known-good run. Update by hand when a
# chain opens or closes stores. Every chain in CRAWLERS needs an entry.
EXPECTED: dict[str, tuple[int, int, int]] = {
    "studenac": (1365, 38_253, 4_850_101),
    "spar": (144, 43_680, 1_939_264),
    "konzum": (573, 50_210, 5_281_672),
    "plodine": (154, 77_819, 6_196_694),
    "lidl": (116, 13_113, 1_959_102),
    "tommy": (87, 24_853, 1_678_723),
    "kaufland": (54, 36_885, 1_083_095),
    "eurospin": (45, 4_082, 246_873),
    "dm": (1, 21_829, 21_829),
    "ktc": (35, 46_914, 606_435),
    "metro": (10, 13_640, 97_491),
    "trgocentar": (95, 80_279, 420_820),
    "zabac": (2, 2_033, 4_066),
    "vrutak": (9, 17_087, 76_778),
    "ntl": (343, 18_428, 1_606_748),
    "ribola": (106, 29_931, 952_803),
    "roto": (9, 9_201, 82_949),
    "trgovina-krk": (14, 34_068, 156_733),
    "brodokomerc": (1, 4_065, 4_065),
    "lorenco": (6, 31_322, 60_123),
    "boso": (283, 12_705, 976_489),
    "jadranka_trgovina": (19, 33_552, 165_771),
    "gavranovic": (179, 16_048, 1_779_616),
    "branka": (2, 16_757, 29_076),
    "djelo_vodice": (37, 20_373, 474_433),
    "bure": (3, 17_283, 30_888),
    "dukat": (6, 415, 4_797),
    "stanic": (31, 2_613, 50_714),
    "stridon": (41, 31_945, 148_412),
    "pivac": (186, 9_543, 1_774_998),
    "bipa": (153, 25_458, 3_895_074),
    "zoocity": (66, 8_202, 316_453),
    "petcentar": (28, 17_269, 483_532),
    "inovine": (193, 9_662, 1_296_701),
}

# Crawler processes run from here, so `-m crawler.cli.crawl` resolves.
PROJECT_DIR = Path(__file__).resolve().parents[2]

LOG_FORMAT = "%(asctime)s:%(name)s:%(levelname)s:%(message)s"


class Counts(NamedTuple):
    stores: int
    products: int
    prices: int

    def rank(self) -> tuple[int, int, int]:
        """Order in which two attempts are compared: prices first."""
        return (self.prices, self.stores, self.products)

    def __str__(self) -> str:
        return f"{self.stores} stores, {self.products} products, {self.prices} prices"


@dataclass
class Attempt:
    exit_code: int
    seconds: float
    max_rss_kb: int | None
    counts: Counts | None
    replaced: bool


@dataclass(frozen=True)
class Paths:
    root: Path
    day: date
    archive_dir: Path

    @property
    def data(self) -> Path:
        return self.root / "data" / f"{self.day:%Y-%m-%d}"

    @property
    def staging_root(self) -> Path:
        """Output root passed to the crawler, which appends `<DATE>/<chain>`."""
        return self.root / "staging"

    @property
    def staging(self) -> Path:
        return self.staging_root / f"{self.day:%Y-%m-%d}"

    @property
    def logs(self) -> Path:
        return self.root / "logs" / f"{self.day:%Y-%m-%d}"

    @property
    def archive(self) -> Path:
        return self.archive_dir / f"{self.day:%Y-%m-%d}.zip"


def count_lines(path: Path) -> int:
    with open(path, "rb") as f:
        return sum(chunk.count(b"\n") for chunk in iter(lambda: f.read(1 << 20), b""))


def count_rows(chain_dir: Path) -> Counts | None:
    """
    Row counts of a chain's CSV files, or None if any of them is missing.

    `save_csv` normalizes whitespace, so each row is one line.
    """
    paths = [chain_dir / f"{name}.csv" for name in Counts._fields]
    if not all(path.is_file() for path in paths):
        return None
    return Counts(*(max(count_lines(path) - 1, 0) for path in paths))


def is_complete(chain: str, counts: Counts | None) -> bool:
    if counts is None:
        return False
    return all(
        actual >= THRESHOLD * expected
        for actual, expected in zip(counts, EXPECTED[chain])
    )


def should_replace(new: Counts | None, current: Counts | None, force: bool) -> bool:
    """Decide if a new attempt replaces the chain's current data."""
    if new is None or new.stores == 0:
        return False
    if force or current is None:
        return True
    return new.rank() > current.rank()


def seed_from_archive(paths: Paths) -> None:
    """
    Start the data directory from the published ZIP.

    Without this, a run limited to some chains would publish a ZIP with only
    those chains over the full one. The ZIP is extracted in staging and then
    renamed, so an interruption cannot leave a partial data directory. The
    directory gets the ZIP's mtime, so the same data is not published again.
    """
    logger.info(f"Starting {paths.data} from {paths.archive}")
    tmp = paths.staging / ".seed"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    with ZipFile(paths.archive) as zf:
        zf.extractall(tmp)
    paths.data.parent.mkdir(parents=True, exist_ok=True)
    tmp.rename(paths.data)
    mtime = paths.archive.stat().st_mtime_ns
    os.utime(paths.data, ns=(mtime, mtime))


def recover_swaps(paths: Paths) -> None:
    """Finish the swaps (see `swap`) that an earlier run did not complete."""
    if not paths.staging.is_dir():
        return
    for old in paths.staging.glob("*.old"):
        live = paths.data / old.name.removesuffix(".old")
        if live.exists():
            shutil.rmtree(old)
        else:
            logger.warning(f"Restoring {live} from an interrupted swap")
            old.rename(live)


def prepare(paths: Paths) -> None:
    if not paths.data.exists():
        if paths.archive.is_file():
            seed_from_archive(paths)
        else:
            paths.data.mkdir(parents=True)
    recover_swaps(paths)


def swap(paths: Paths, chain: str) -> None:
    """Replace the chain's data with the new attempt from staging."""
    live = paths.data / chain
    new = paths.staging / chain
    old = paths.staging / f"{chain}.old"
    if old.exists():
        shutil.rmtree(old)
    if live.exists():
        live.rename(old)
    new.rename(live)
    shutil.rmtree(old, ignore_errors=True)


def needs_publish(paths: Paths) -> bool:
    """
    True if the ZIP is missing or older than the data directory.

    A rename into or out of the data directory updates its mtime, so this
    also catches a replace from an earlier run that stopped before the ZIP.
    """
    if not any(entry.is_dir() for entry in paths.data.iterdir()):
        return False
    if not paths.archive.exists():
        return True
    return paths.data.stat().st_mtime_ns > paths.archive.stat().st_mtime_ns


def publish(paths: Paths) -> None:
    """
    Build the ZIP next to the target and rename it into place.

    The temp name is unique, so two overlapping runs do not write to the same
    file. The API lists only `*.zip` files, so it does not show the temp file.
    """
    logger.info(f"Publishing {paths.archive}")
    paths.archive_dir.mkdir(parents=True, exist_ok=True)
    suffix = secrets.token_hex(4)
    tmp = paths.archive.with_name(f"{paths.archive.name}.tmp.{suffix}")
    copy_archive_info(paths.data)
    try:
        create_archive(paths.data, tmp)
        os.replace(tmp, paths.archive)
    finally:
        tmp.unlink(missing_ok=True)


def crawler_command(chain: str, day: date, staging_root: Path) -> list[str]:
    return [
        sys.executable,
        "-m",
        "crawler.cli.crawl",
        "-v",
        "info",
        "-z",
        "false",
        "-d",
        f"{day:%Y-%m-%d}",
        "-c",
        chain,
        str(staging_root),
    ]


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def reap(proc: subprocess.Popen) -> tuple[int, int | None] | None:
    """
    Exit code and peak RSS in KB of a process that has exited, or None if it
    is still running. Windows does not report the peak RSS.
    """
    if sys.platform == "win32":
        exit_code = proc.poll()
        return None if exit_code is None else (exit_code, None)
    pid, status, rusage = os.wait4(proc.pid, os.WNOHANG)
    if pid == 0:
        return None
    # Already reaped by wait4; tell Popen so it does not wait again.
    proc.returncode = os.waitstatus_to_exitcode(status)
    # ru_maxrss is in bytes on macOS, in KB on Linux.
    max_rss_kb = rusage.ru_maxrss // (1024 if sys.platform == "darwin" else 1)
    return proc.returncode, max_rss_kb


def kill(proc: subprocess.Popen) -> None:
    if sys.platform == "win32":
        proc.kill()
    else:
        # Not Popen.kill(): it polls, and would reap the process before wait4
        # sees it.
        os.kill(proc.pid, signal.SIGKILL)


def crawl_chains(
    paths: Paths, chains: list[str], force: set[str]
) -> dict[str, Attempt]:
    """
    Start one crawler process per chain and decide each chain when it exits.
    """
    paths.logs.mkdir(parents=True, exist_ok=True)
    paths.staging.mkdir(parents=True, exist_ok=True)

    running: dict[str, tuple[subprocess.Popen, float, IO[str]]] = {}
    for chain in chains:
        shutil.rmtree(paths.staging / chain, ignore_errors=True)
        log = open(paths.logs / f"{chain}.log", "a")
        log.write(f"=== {now()} start {chain}, date {paths.day:%Y-%m-%d}\n")
        log.flush()
        proc = subprocess.Popen(
            crawler_command(chain, paths.day, paths.staging_root),
            cwd=PROJECT_DIR,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        running[chain] = (proc, time(), log)

    attempts: dict[str, Attempt] = {}
    killed: set[str] = set()
    while running:
        for chain, (proc, started, log) in list(running.items()):
            result = reap(proc)
            if result is None:
                if chain not in killed and time() - started > CHAIN_TIMEOUT:
                    logger.warning(
                        f"{chain}: running for {CHAIN_TIMEOUT} s, killing it"
                    )
                    log.write(f"=== {now()} timeout after {CHAIN_TIMEOUT} s, kill\n")
                    log.flush()
                    kill(proc)
                    killed.add(chain)
                continue

            del running[chain]
            exit_code, max_rss_kb = result

            # A non-zero exit means a crash or a kill, so the output can be
            # partial.
            new = count_rows(paths.staging / chain) if exit_code == 0 else None
            current = count_rows(paths.data / chain)
            replaced = should_replace(new, current, chain in force)
            if replaced:
                swap(paths, chain)
            else:
                shutil.rmtree(paths.staging / chain, ignore_errors=True)

            outcome = "replaced" if replaced else "discarded"
            log.write(f"=== {now()} exit {exit_code}: {new or 'no data'}, {outcome}\n")
            log.close()
            logger.info(f"{chain}: exit {exit_code}, {new or 'no data'}, {outcome}")

            attempts[chain] = Attempt(
                exit_code=exit_code,
                seconds=time() - started,
                max_rss_kb=max_rss_kb,
                counts=new,
                replaced=replaced,
            )

        if running:
            sleep(1)

    return attempts


def run(paths: Paths, chains: list[str], force: list[str]) -> dict[str, Attempt]:
    """Crawl the incomplete and the forced chains, then publish if needed."""
    prepare(paths)

    selected = [
        chain
        for chain in chains
        if chain in force or not is_complete(chain, count_rows(paths.data / chain))
    ]
    selected += [chain for chain in force if chain not in selected]

    attempts: dict[str, Attempt] = {}
    if selected:
        logger.info(
            f"Crawling {len(selected)} chain(s) for {paths.day:%Y-%m-%d}: "
            f"{', '.join(selected)}"
        )
        attempts = crawl_chains(paths, selected, set(force))
    else:
        logger.info(f"All chains are complete for {paths.day:%Y-%m-%d}")

    if needs_publish(paths):
        publish(paths)

    return attempts


def chain_status(paths: Paths, chains: list[str]) -> dict[str, dict]:
    status = {}
    for chain in chains:
        counts = count_rows(paths.data / chain)
        status[chain] = {
            "complete": is_complete(chain, counts),
            "actual": counts._asdict() if counts else None,
            "expected": Counts(*EXPECTED[chain])._asdict(),
        }
    return status


def format_cell(actual: int | None, expected: int) -> str:
    if actual is None:
        return f"-/{expected}"
    if expected == 0:
        return f"{actual}/{expected}"
    return f"{actual}/{expected} {actual / expected:4.0%}"


def format_attempt(attempt: Attempt | None) -> str:
    if attempt is None:
        return ""
    outcome = "replaced" if attempt.replaced else "discarded"
    if attempt.exit_code != 0:
        outcome += f" (exit {attempt.exit_code})"
    parts = [outcome, f"{attempt.seconds:.0f} s"]
    if attempt.max_rss_kb is not None:
        parts.append(f"{attempt.max_rss_kb / 1024**2:.1f} GB")
    return ", ".join(parts)


def format_table(
    day: date, status: dict[str, dict], attempts: dict[str, Attempt]
) -> str:
    widths = {"stores": 16, "products": 18, "prices": 22}
    header = f"{'chain':<18}" + "".join(f"{f:>{w}}" for f, w in widths.items())
    lines = [
        f"Status for {day:%Y-%m-%d} (complete: each count >= {THRESHOLD:.0%})",
        f"{header}  {'complete':<9}attempt",
    ]
    for chain, s in status.items():
        actual = s["actual"]
        cells = "".join(
            f"{format_cell(actual[f] if actual else None, s['expected'][f]):>{w}}"
            for f, w in widths.items()
        )
        complete = "yes" if s["complete"] else "no"
        attempt = format_attempt(attempts.get(chain))
        lines.append(f"{chain:<18}{cells}  {complete:<9}{attempt}".rstrip())

    n_complete = sum(1 for s in status.values() if s["complete"])
    n_replaced = sum(1 for a in attempts.values() if a.replaced)
    lines.append(
        f"{n_complete}/{len(status)} complete; "
        f"crawled {len(attempts)}, replaced {n_replaced}"
    )
    return "\n".join(lines)


def cleanup(root: Path, keep_days: int, today: date) -> None:
    """Delete data, staging and logs for dates older than `keep_days`."""
    cutoff = today - timedelta(days=keep_days)
    for sub in ("data", "staging", "logs"):
        base = root / sub
        if not base.is_dir():
            continue
        for entry in sorted(base.iterdir()):
            try:
                day = date.fromisoformat(entry.name)
            except ValueError:
                continue
            if day < cutoff and entry.is_dir():
                logger.info(f"Deleting {entry}")
                shutil.rmtree(entry)


def parse_chains(parser: argparse.ArgumentParser, value: str | None) -> list[str]:
    if not value:
        return []
    chains = [chain.strip() for chain in value.split(",") if chain.strip()]
    unknown = [chain for chain in chains if chain not in get_chains()]
    if unknown:
        parser.error(f"Unknown chain(s): {', '.join(unknown)}")
    return chains


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    commands = parser.add_subparsers(dest="command", required=True)

    run_parser = commands.add_parser(
        "run", help="crawl missing or incomplete chains, publish the ZIP"
    )
    run_parser.add_argument("root", type=Path, help="state directory")
    run_parser.add_argument(
        "-d", "--date", type=date.fromisoformat, help="YYYY-MM-DD (default: today)"
    )
    run_parser.add_argument(
        "-c", "--chain", help="comma-separated chains to consider (default: all)"
    )
    run_parser.add_argument(
        "--force",
        help="comma-separated chains to crawl even if complete; the result "
        "replaces the current data if it has at least one store",
    )
    run_parser.add_argument(
        "--archive-dir", type=Path, help="where to publish the ZIP (default: root)"
    )

    status_parser = commands.add_parser(
        "status", help="print the status of a date without crawling"
    )
    status_parser.add_argument("root", type=Path, help="state directory")
    status_parser.add_argument(
        "-d", "--date", type=date.fromisoformat, help="YYYY-MM-DD (default: today)"
    )
    status_parser.add_argument("--json", action="store_true", help="print JSON")

    cleanup_parser = commands.add_parser(
        "cleanup", help="delete data, staging and logs of old dates (not ZIPs)"
    )
    cleanup_parser.add_argument("root", type=Path, help="state directory")
    cleanup_parser.add_argument(
        "--keep-days", type=int, default=7, help="days to keep (default: 7)"
    )

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, stream=sys.stderr)

    root = args.root.resolve()
    today = date.today()

    if args.command == "cleanup":
        if args.keep_days < 0:
            parser.error("--keep-days must be 0 or more")
        cleanup(root, args.keep_days, today)
        return 0

    missing = [chain for chain in get_chains() if chain not in EXPECTED]
    if missing:
        parser.error(f"No EXPECTED entry for: {', '.join(missing)}")

    day = args.date or today
    all_chains = get_chains()

    if args.command == "status":
        paths = Paths(root, day, root)
        status = chain_status(paths, all_chains)
        if args.json:
            data = {"date": f"{day:%Y-%m-%d}", "threshold": THRESHOLD}
            print(json.dumps(data | {"chains": status}, indent=2))
        else:
            print(format_table(day, status, {}))
        return 0

    archive_dir = (args.archive_dir or root).resolve()
    paths = Paths(root, day, archive_dir)
    chains = parse_chains(parser, args.chain) or all_chains
    force = parse_chains(parser, args.force)

    attempts = run(paths, chains, force)
    print(format_table(day, chain_status(paths, all_chains), attempts), flush=True)
    if not paths.archive.exists():
        # Fail, so that the import of an empty directory does not run.
        logger.error(f"No chain has data for {day:%Y-%m-%d}, no ZIP published")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
