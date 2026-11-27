import datetime
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from time import time
from typing import List

from crawler.store.base import CrawlerBlocked
from crawler.store.bipa import BipaCrawler
from crawler.store.boso import BosoCrawler
from crawler.store.branka import BrankaCrawler
from crawler.store.brodokomerc import BrodokomercCrawler
from crawler.store.bure import BureCrawler
from crawler.store.djelo_vodice import DjeloVodiceCrawler
from crawler.store.dm import DmCrawler
from crawler.store.dukat import DukatCrawler
from crawler.store.eurospin import EurospinCrawler
from crawler.store.gavranovic import GavranovicCrawler
from crawler.store.inovine import INovineCrawler
from crawler.store.jadranka_trgovina import JadrankaTrgovinaCrawler
from crawler.store.kaufland import KauflandCrawler
from crawler.store.konzum import KonzumCrawler
from crawler.store.ktc import KtcCrawler
from crawler.store.lidl import LidlCrawler
from crawler.store.lorenco import LorencoCrawler
from crawler.store.metro import MetroCrawler
from crawler.store.ntl import NtlCrawler
from crawler.store.output import ChainWriter, copy_archive_info, create_archive
from crawler.store.petcentar import PetCentarCrawler
from crawler.store.pivac import PivacCrawler
from crawler.store.plodine import PlodineCrawler
from crawler.store.ribola import RibolaCrawler
from crawler.store.roto import RotoCrawler
from crawler.store.spar import SparCrawler
from crawler.store.stanic import StanicCrawler
from crawler.store.stridon import StridonCrawler
from crawler.store.studenac import StudenacCrawler
from crawler.store.tommy import TommyCrawler
from crawler.store.trgocentar import TrgocentarCrawler
from crawler.store.trgovina_krk import TrgovinaKrkCrawler
from crawler.store.vrutak import VrutakCrawler
from crawler.store.zoocity import ZooCityCrawler
from crawler.store.zabac import ZabacCrawler

logger = logging.getLogger(__name__)

CRAWLERS = {
    StudenacCrawler.CHAIN: StudenacCrawler,
    SparCrawler.CHAIN: SparCrawler,
    KonzumCrawler.CHAIN: KonzumCrawler,
    PlodineCrawler.CHAIN: PlodineCrawler,
    LidlCrawler.CHAIN: LidlCrawler,
    TommyCrawler.CHAIN: TommyCrawler,
    KauflandCrawler.CHAIN: KauflandCrawler,
    EurospinCrawler.CHAIN: EurospinCrawler,
    DmCrawler.CHAIN: DmCrawler,
    KtcCrawler.CHAIN: KtcCrawler,
    MetroCrawler.CHAIN: MetroCrawler,
    TrgocentarCrawler.CHAIN: TrgocentarCrawler,
    ZabacCrawler.CHAIN: ZabacCrawler,
    VrutakCrawler.CHAIN: VrutakCrawler,
    NtlCrawler.CHAIN: NtlCrawler,
    RibolaCrawler.CHAIN: RibolaCrawler,
    RotoCrawler.CHAIN: RotoCrawler,
    TrgovinaKrkCrawler.CHAIN: TrgovinaKrkCrawler,
    BrodokomercCrawler.CHAIN: BrodokomercCrawler,
    LorencoCrawler.CHAIN: LorencoCrawler,
    BosoCrawler.CHAIN: BosoCrawler,
    JadrankaTrgovinaCrawler.CHAIN: JadrankaTrgovinaCrawler,
    GavranovicCrawler.CHAIN: GavranovicCrawler,
    BrankaCrawler.CHAIN: BrankaCrawler,
    DjeloVodiceCrawler.CHAIN: DjeloVodiceCrawler,
    BureCrawler.CHAIN: BureCrawler,
    DukatCrawler.CHAIN: DukatCrawler,
    StanicCrawler.CHAIN: StanicCrawler,
    StridonCrawler.CHAIN: StridonCrawler,
    PivacCrawler.CHAIN: PivacCrawler,
    BipaCrawler.CHAIN: BipaCrawler,
    ZooCityCrawler.CHAIN: ZooCityCrawler,
    PetCentarCrawler.CHAIN: PetCentarCrawler,
    INovineCrawler.CHAIN: INovineCrawler,
}


def get_chains() -> List[str]:
    """
    Get the list of retail chains from the crawlers.

    Returns:
        List of retail chain names.
    """
    return list(CRAWLERS.keys())


@dataclass
class CrawlResult:
    elapsed_time: float = 0
    n_stores: int = 0
    n_products: int = 0
    n_prices: int = 0


def crawl_chain(chain: str, date: datetime.date, path: Path) -> CrawlResult:
    """
    Crawl a specific retail chain for product/pricing data and save it.

    Args:
        chain: The name of the retail chain to crawl.
        date: The date for which to fetch the product data.
        path: The directory path where the data will be saved.
    """

    crawler_class = CRAWLERS.get(chain)
    if not crawler_class:
        raise ValueError(f"Unknown retail chain: {chain}")

    crawler = crawler_class()
    writer = ChainWriter(path)
    t0 = time()

    stores = crawler.iter_stores(date)
    try:
        while True:
            try:
                store = next(stores)
            except StopIteration:
                break
            except Exception as err:
                logger.error(
                    f"Error crawling {chain} for {date:%Y-%m-%d}, keeping the "
                    f"{writer.n_stores} store(s) before it: {err}",
                    exc_info=not isinstance(err, CrawlerBlocked),
                )
                break
            writer.add(store)
    except BaseException:
        # A writer error (or an interrupt) fails the chain and writes nothing
        writer.abort()
        raise
    finally:
        # Ends the crawler early if the loop stopped, removing its temp files
        stores.close()

    if crawler.blocked:
        logger.error(
            f"{chain} was blocked part-way through {date:%Y-%m-%d}: "
            f"keeping the {writer.n_stores} store(s) collected before the block, "
            f"this chain is incomplete"
        )

    if not writer.n_stores:
        logger.error(f"No stores imported for {chain} on {date}")
        return CrawlResult()

    writer.finish()
    t1 = time()

    return CrawlResult(
        elapsed_time=t1 - t0,
        n_stores=writer.n_stores,
        n_products=writer.n_products,
        n_prices=writer.n_prices,
    )


def crawl(
    root: Path,
    date: datetime.date | None = None,
    chains: list[str] | None = None,
    createzip: bool = True,
) -> None:
    """
    Crawl multiple retail chains for product/pricing data and save it.

    Args:
        root: The base directory path where the data will be saved.
        date: The date for which to fetch the product data. If None, uses today's date.
        chains: List of retail chain names to crawl. If None, crawls all available chains.

    Returns:
        Path to the created ZIP archive file.
    """

    if chains is None:
        chains = get_chains()

    if date is None:
        date = datetime.date.today()

    path = root / date.strftime("%Y-%m-%d")
    zip_path = root / f"{date:%Y-%m-%d}.zip"
    os.makedirs(path, exist_ok=True)

    results = {}

    t0 = time()
    for chain in chains:
        logger.info(f"Starting crawl for {chain} on {date:%Y-%m-%d}")
        r = crawl_chain(chain, date, path / chain)
        results[chain] = r
    t1 = time()

    logger.info(f"Crawled {','.join(chains)} for {date:%Y-%m-%d} in {t1 - t0:.2f}s")
    for chain, r in results.items():
        logger.info(
            f"  * {chain}: {r.n_stores} stores, {r.n_products} products, {r.n_prices} prices in {r.elapsed_time:.2f}s"
        )

    if createzip is False:
        return

    copy_archive_info(path)
    create_archive(path, zip_path)

    logger.info(f"Created archive {zip_path} with data for {date:%Y-%m-%d}")
    print(f"Archive created: {zip_path}")
