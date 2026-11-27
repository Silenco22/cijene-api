import datetime
import logging
import re
from urllib.parse import unquote, urljoin

from bs4 import BeautifulSoup

from crawler.store.models import Store

from .base import BaseCrawler, CrawlerBlocked

logger = logging.getLogger(__name__)


class ZooCityCrawler(BaseCrawler):
    """
    Crawler for ZooCity (pet shops).

    The index page has a store dropdown. Each store's page (?location=<code>)
    links one CSV per day, with the date in the file name. The file names
    carry the address without a separator between street and city, so the
    store details come from enrichment/stores.csv, keyed by the dropdown code.
    The web shop has its own price list and is kept as a store.

    The files use the NN 75/2025 columns (comma-separated, decimal point) with
    a separate sale price, not the rulebook 1584 format.
    """

    CHAIN = "zoocity"
    INDEX_URL = "https://www.zoocity.hr/pricelist/"

    # trgovina_Turinina_2a_Zagreb_02_10_26.11.2026.csv
    # WEBSHOP_CJENIK_26.11.2026.csv
    FILE_PATTERN = re.compile(r"(?:_(\d+))?_(\d{2}\.\d{2}\.\d{4})\.csv$")

    WEBSHOP = "webshop"

    PRICE_MAP = {
        "price": ("MALOPRODAJNA_CIJENA", False),
        "unit_price": ("CIJENA_ZA_JEDINICU_MJERE", False),
        "special_price": ("AKCIJSKA_CIJENA", False),
        "anchor_price": ("SIDRENA_CIJENA", False),
    }

    FIELD_MAP = {
        "product": ("NAZIV", True),
        "product_id": ("ŠIFRA", True),
        "brand": ("MARKA", False),
        "quantity": ("", False),  # not published, part of the name
        "unit": ("JEDINICA_MJERE", False),
        "barcode": ("BARKOD", False),
        "category": ("", False),
        "special_sale_type": ("VRSTA_POSEBNOG_OBLIKA_PRODAJE", False),
    }

    BOOL_MAP = {
        "available": ("DOSTUPNOST", False),
    }

    # Without AKCIJSKA_CIJENA, sale prices would be lost without a warning
    REQUIRED_COLUMNS = [
        "NAZIV",
        "ŠIFRA",
        "MARKA",
        "JEDINICA_MJERE",
        "MALOPRODAJNA_CIJENA",
        "AKCIJSKA_CIJENA",
        "SIDRENA_CIJENA",
        "BARKOD",
    ]

    # Rulebook 1584 (NN 132/2026) requires neither availability nor the sale
    # name, and the unit price only "ako je primjenjivo".
    OPTIONAL_COLUMNS = [
        "CIJENA_ZA_JEDINICU_MJERE",
        "VRSTA_POSEBNOG_OBLIKA_PRODAJE",
        "DOSTUPNOST",
    ]

    # "SIDRENA_CIJENA" states no date
    ANCHOR_PRICE_DATE = None

    def __init__(self):
        super().__init__()
        self._skipped_zero_price = 0

    def get_stores(self) -> dict[str, str]:
        """
        Read the stores from the dropdown on the index page.

        Returns:
            Store name by store code
        """
        soup = BeautifulSoup(self.fetch_text(self.INDEX_URL), "html.parser")
        stores = {}
        for option in soup.select("select#location option[value]"):
            code = str(option["value"]).strip()
            if not code:
                continue

            # The web shop's option text is just "Webshop"
            if code == self.WEBSHOP:
                stores[code] = "ZooCity Webshop"
                continue

            # "ZOOCITY Zagreb - Sopot" -> "ZooCity Zagreb - Sopot"
            # "HUNTER powered by ZOOCITY - Zagreb Centar Cvjetni"
            #   -> "Hunter Zagreb Centar Cvjetni"
            name = " ".join(option.get_text().split())
            name = name.replace("HUNTER powered by ZOOCITY -", "Hunter")
            stores[code] = name.replace("ZOOCITY", "ZooCity")
        return stores

    def get_store_url(self, code: str, date: datetime.date) -> str | None:
        """
        Find the store's CSV for the date on the store's page.

        Args:
            code: Store code
            date: The date of the price list

        Returns:
            CSV URL, or None if the store has no price list for the date
        """
        page_url = f"{self.INDEX_URL}?location={code}"
        soup = BeautifulSoup(self.fetch_text(page_url), "html.parser")
        day = f"{date:%d.%m.%Y}"

        candidates = []
        for link in soup.select('a[href$=".csv"]'):
            url = urljoin(page_url, str(link["href"]))
            filename = unquote(url.rsplit("/", 1)[-1])
            m = self.FILE_PATTERN.search(filename)
            if not m:
                logger.warning(f"Skipping {filename}: unexpected file name")
                continue

            number, file_day = m.groups()
            if file_day == day:
                candidates.append((int(number or 0), url))

        if not candidates:
            return None

        # Not seen so far. The number is a day counter, so this is a guess.
        if len(candidates) > 1:
            logger.info(f"ZooCity {code} has more than one file for {date}")
        return max(candidates)[1]

    def keep_row(self, row: dict[str, str]) -> bool:
        # Price 0.00 and no sale price: the row has only an anchor price
        price = self.parse_price(row.get("MALOPRODAJNA_CIJENA"), required=False)
        special = self.parse_price(row.get("AKCIJSKA_CIJENA"), required=False)
        if price == 0 and not special:
            self._skipped_zero_price += 1
            return False
        return True

    def get_all_products(self, date: datetime.date) -> list[Store]:
        stores = []
        missing = []
        for code, name in sorted(self.get_stores().items()):
            try:
                url = self.get_store_url(code, date)
                if url is None:
                    missing.append(code)
                    continue

                content = self.fetch_text(url, encodings=["utf-8"])
                products = self.parse_csv(content)
            except CrawlerBlocked:
                raise
            except Exception as e:
                logger.error(f"Error processing ZooCity {code}: {e}", exc_info=True)
                continue

            store = self.get_mapped_store(code, name)
            store.items = products
            stores.append(store)

        if not stores:
            logger.info(f"No ZooCity price list for {date}")
            return []

        if missing:
            logger.info(f"ZooCity: no price list for {date} in {', '.join(missing)}")
        logger.info(
            f"ZooCity: skipped {self._skipped_zero_price} rows with price 0 "
            "and no sale price"
        )
        return stores


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    crawler = ZooCityCrawler()
    stores = crawler.get_all_products(datetime.date.today())
    print(stores[0])
    print(stores[0].items[0])
