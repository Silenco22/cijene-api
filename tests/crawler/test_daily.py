import os
import sys
from datetime import date
from pathlib import Path
from zipfile import ZipFile

import pytest

from crawler.cli import daily
from crawler.cli.daily import Counts, Paths
from crawler.crawl import get_chains
from crawler.store.output import create_archive

DAY = date(2026, 11, 24)

# The tests below replace EXPECTED with small numbers.
REAL_EXPECTED = daily.EXPECTED

# Writes `stores` store rows and `prices` product and price rows to the chain
# directory (no files if `stores` is 0), sleeps `seconds`, then exits with
# `exit_code`.
FAKE_CRAWLER = """
import sys
import time
from pathlib import Path

out, stores, prices, exit_code, seconds = Path(sys.argv[1]), *map(int, sys.argv[2:6])
if stores:
    out.mkdir(parents=True)
    for name, n in (("stores", stores), ("products", prices), ("prices", prices)):
        (out / f"{name}.csv").write_text("header\\n" + "row\\n" * n)
time.sleep(seconds)
sys.exit(exit_code)
"""


def write_chain(chain_dir: Path, stores: int, products: int, prices: int) -> None:
    chain_dir.mkdir(parents=True)
    for name, n in (("stores", stores), ("products", products), ("prices", prices)):
        (chain_dir / f"{name}.csv").write_text("header\r\n" + "row\r\n" * n)


def set_mtime(path: Path, seconds_ago: int) -> None:
    t = path.stat().st_mtime_ns - seconds_ago * 10**9
    os.utime(path, ns=(t, t))


def zip_counts(archive: Path) -> dict[str, int]:
    """Number of price rows per chain in a ZIP."""
    with ZipFile(archive) as zf:
        return {
            name.split("/")[0]: len(zf.read(name).splitlines()) - 1
            for name in zf.namelist()
            if name.endswith("/prices.csv")
        }


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    return Paths(tmp_path / "root", DAY, tmp_path / "public")


@pytest.fixture(autouse=True)
def expected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        daily,
        "EXPECTED",
        {"konzum": (10, 100, 1000), "spar": (5, 50, 500), "lidl": (5, 50, 500)},
    )


@pytest.fixture
def fake_crawler(monkeypatch: pytest.MonkeyPatch):
    """Set the fake result per chain: (stores, prices[, exit code[, seconds]])."""
    results: dict[str, tuple[int, ...]] = {}

    def command(chain: str, day: date, staging_root: Path) -> list[str]:
        stores, prices, *rest = results[chain]
        exit_code, seconds = [*rest, 0, 0][:2]
        out = staging_root / f"{day:%Y-%m-%d}" / chain
        args = [str(out), str(stores), str(prices), str(exit_code), str(seconds)]
        return [sys.executable, "-c", FAKE_CRAWLER, *args]

    monkeypatch.setattr(daily, "crawler_command", command)
    return results


def test_expected_covers_all_chains():
    assert set(REAL_EXPECTED) == set(get_chains())


def test_count_rows(tmp_path: Path):
    write_chain(tmp_path / "konzum", 2, 3, 4)
    assert daily.count_rows(tmp_path / "konzum") == Counts(2, 3, 4)
    (tmp_path / "konzum" / "products.csv").unlink()
    assert daily.count_rows(tmp_path / "konzum") is None


@pytest.mark.parametrize(
    "counts, complete",
    [
        (Counts(10, 100, 1000), True),
        (Counts(9, 90, 900), True),
        (Counts(20, 200, 2000), True),
        (Counts(8, 100, 1000), False),
        (Counts(10, 89, 1000), False),
        (Counts(10, 100, 899), False),
        (None, False),
    ],
)
def test_is_complete(counts: Counts | None, complete: bool):
    assert daily.is_complete("konzum", counts) is complete


@pytest.mark.parametrize(
    "new, current, force, replace",
    [
        (Counts(10, 100, 1000), None, False, True),
        (None, Counts(10, 100, 1000), False, False),
        (None, Counts(10, 100, 1000), True, False),
        # Prices first: more stores with fewer prices do not replace.
        (Counts(11, 100, 900), Counts(10, 100, 1000), False, False),
        (Counts(9, 100, 1001), Counts(10, 100, 1000), False, True),
        (Counts(11, 100, 1000), Counts(10, 100, 1000), False, True),
        (Counts(10, 100, 1000), Counts(10, 100, 1000), False, False),
        (Counts(1, 1, 1), Counts(10, 100, 1000), True, True),
    ],
)
def test_should_replace(
    new: Counts | None, current: Counts | None, force: bool, replace: bool
):
    assert daily.should_replace(new, current, force) is replace


def test_run_crawls_only_incomplete_chains(paths: Paths, fake_crawler):
    write_chain(paths.data / "konzum", 10, 100, 1000)
    write_chain(paths.data / "spar", 2, 20, 200)
    fake_crawler.update({"spar": (5, 500), "lidl": (5, 500)})

    attempts = daily.run(paths, ["konzum", "spar", "lidl"], force=[])

    assert set(attempts) == {"spar", "lidl"}
    assert all(a.replaced for a in attempts.values())
    assert zip_counts(paths.archive) == {"konzum": 1000, "spar": 500, "lidl": 500}
    assert [p.name for p in paths.archive_dir.iterdir()] == ["2026-11-24.zip"]
    assert not (paths.staging / "spar").exists()

    log = (paths.logs / "spar.log").read_text()
    assert "start spar, date 2026-11-24" in log
    assert "exit 0: 5 stores, 500 products, 500 prices, replaced" in log


def test_forced_chain_replaces_smaller_data(paths: Paths, fake_crawler):
    write_chain(paths.data / "konzum", 10, 100, 1000)
    fake_crawler["konzum"] = (1, 10)

    attempts = daily.run(paths, ["konzum"], force=["konzum"])

    assert attempts["konzum"].replaced
    assert daily.count_rows(paths.data / "konzum") == Counts(1, 10, 10)


def test_failed_attempt_is_discarded(paths: Paths, fake_crawler):
    write_chain(paths.data / "konzum", 5, 50, 500)
    fake_crawler["konzum"] = (10, 1000, 1)

    attempts = daily.run(paths, ["konzum"], force=[])

    assert attempts["konzum"].exit_code == 1
    assert not attempts["konzum"].replaced
    assert daily.count_rows(paths.data / "konzum") == Counts(5, 50, 500)
    assert not (paths.staging / "konzum").exists()


def test_stuck_chain_is_killed(
    paths: Paths, fake_crawler, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(daily, "CHAIN_TIMEOUT", 2)
    fake_crawler.update({"konzum": (10, 1000, 0, 60), "spar": (5, 500)})

    attempts = daily.run(paths, ["konzum", "spar"], force=[])

    assert attempts["konzum"].exit_code != 0
    assert not attempts["konzum"].replaced
    assert attempts["konzum"].seconds < 30
    assert zip_counts(paths.archive) == {"spar": 500}
    assert "timeout after 2 s" in (paths.logs / "konzum.log").read_text()


def test_one_chain_run_keeps_other_chains_of_existing_zip(
    paths: Paths, fake_crawler, tmp_path: Path
):
    """A date with a published ZIP but no data directory starts from the ZIP."""
    source = tmp_path / "source"
    write_chain(source / "konzum", 5, 50, 500)
    write_chain(source / "spar", 5, 50, 500)
    paths.archive_dir.mkdir()
    create_archive(source, paths.archive)
    set_mtime(paths.archive, 60)
    fake_crawler["konzum"] = (10, 1000)

    daily.run(paths, ["konzum"], force=[])

    assert zip_counts(paths.archive) == {"konzum": 1000, "spar": 500}


def test_seeding_from_zip_does_not_republish(paths: Paths, tmp_path: Path):
    source = tmp_path / "source"
    write_chain(source / "konzum", 10, 100, 1000)
    paths.archive_dir.mkdir()
    create_archive(source, paths.archive)
    set_mtime(paths.archive, 60)
    mtime = paths.archive.stat().st_mtime_ns

    attempts = daily.run(paths, ["konzum"], force=[])

    assert attempts == {}
    assert daily.count_rows(paths.data / "konzum") == Counts(10, 100, 1000)
    assert paths.archive.stat().st_mtime_ns == mtime


def test_interrupted_swap_is_restored(paths: Paths):
    write_chain(paths.staging / "konzum.old", 10, 100, 1000)
    write_chain(paths.staging / "spar.old", 1, 1, 1)
    write_chain(paths.data / "spar", 5, 50, 500)

    daily.prepare(paths)

    assert daily.count_rows(paths.data / "konzum") == Counts(10, 100, 1000)
    assert daily.count_rows(paths.data / "spar") == Counts(5, 50, 500)
    assert list(paths.staging.glob("*.old")) == []


def test_zip_behind_data_is_published_without_crawling(paths: Paths):
    """A run that replaced a chain and stopped before the ZIP."""
    write_chain(paths.data / "konzum", 5, 50, 500)
    paths.archive_dir.mkdir()
    create_archive(paths.data, paths.archive)
    set_mtime(paths.archive, 60)
    write_chain(paths.staging / "konzum", 10, 100, 1000)
    daily.swap(paths, "konzum")

    attempts = daily.run(paths, ["konzum"], force=[])

    assert attempts == {}
    assert zip_counts(paths.archive) == {"konzum": 1000}


def test_unchanged_rerun_does_not_rezip(paths: Paths, fake_crawler):
    write_chain(paths.data / "konzum", 5, 500, 500)
    paths.archive_dir.mkdir()
    create_archive(paths.data, paths.archive)
    set_mtime(paths.data, 60)
    mtime = paths.archive.stat().st_mtime_ns
    fake_crawler["konzum"] = (5, 500)

    attempts = daily.run(paths, ["konzum"], force=[])

    assert not attempts["konzum"].replaced
    assert paths.archive.stat().st_mtime_ns == mtime


def test_empty_data_is_not_published(paths: Paths, fake_crawler):
    fake_crawler["konzum"] = (0, 0)

    daily.run(paths, ["konzum"], force=[])

    assert not paths.archive.exists()


def test_run_fails_when_no_zip_exists(
    paths: Paths, fake_crawler, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(daily, "get_chains", lambda: ["konzum"])
    argv = ["daily", "run", str(paths.root), "-d", f"{DAY}"]
    monkeypatch.setattr(sys, "argv", [*argv, "--archive-dir", str(paths.archive_dir)])

    fake_crawler["konzum"] = (0, 0, 1)
    assert daily.main() == 1

    fake_crawler["konzum"] = (5, 500)
    assert daily.main() == 0


def test_status_reports_actual_and_expected(paths: Paths):
    write_chain(paths.data / "konzum", 12, 120, 1200)

    status = daily.chain_status(paths, ["konzum", "spar"])

    assert status["konzum"] == {
        "complete": True,
        "actual": {"stores": 12, "products": 120, "prices": 1200},
        "expected": {"stores": 10, "products": 100, "prices": 1000},
    }
    assert status["spar"]["actual"] is None
    assert not status["spar"]["complete"]


def test_cleanup_keeps_recent_dates(tmp_path: Path):
    for sub in ("data", "staging", "logs"):
        for name in ("2026-11-16", "2026-11-17", "2026-11-24", "other"):
            (tmp_path / sub / name).mkdir(parents=True)

    daily.cleanup(tmp_path, keep_days=7, today=DAY)

    for sub in ("data", "staging", "logs"):
        names = sorted(p.name for p in (tmp_path / sub).iterdir())
        assert names == ["2026-11-17", "2026-11-24", "other"]


def test_cleanup_rejects_negative_keep_days(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    current = tmp_path / "data" / f"{date.today()}"
    current.mkdir(parents=True)
    argv = ["daily", "cleanup", str(tmp_path), "--keep-days", "-1"]
    monkeypatch.setattr(sys, "argv", argv)

    with pytest.raises(SystemExit) as exc:
        daily.main()

    assert exc.value.code == 2
    assert current.exists()
