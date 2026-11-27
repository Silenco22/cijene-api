import datetime
import logging
import re
from csv import DictReader
from urllib.parse import unquote, urljoin

from bs4 import BeautifulSoup

from crawler.store.models import Store

from .base import BaseCrawler, CrawlerBlocked

logger = logging.getLogger(__name__)


class BipaCrawler(BaseCrawler):
    """
    Crawler for Bipa (drugstores).

    One index page links a CSV per store for each day still online. The file
    names carry the store's city and street without diacritics and without a
    separator between them, so the store details come from
    enrichment/stores.csv, keyed by the store code in the file name.

    Each CSV has a Croatian header line, then an English one. The Croatian
    header is UTF-8 and the data rows are ISO-8859-2, so UTF-8 decoding fails
    and the file is decoded as ISO-8859-2. UTF-8 is tried first because
    rulebook 1584 (NN 132/2026) requires it. Rows are read from the English
    header, which is ASCII and so the same in both encodings.
    """

    CHAIN = "bipa"
    BASE_URL = "https://bipa.hr"
    INDEX_URL = BASE_URL + "/cjenici/"
    DAY_PATH = "/price-list/csv/{date:%Y-%m-%d}/"

    # [66498917936]_ProdavaonicaBeliManastirTrgSlobode39_P-7205_ROBA_18_20261125_0401.csv
    # "ROBA" is the goods list; a services list ("USLUGE") would not match.
    FILE_PATTERN = re.compile(r"_(P-\d+)_ROBA_(\d+)_\d{8}_(\d{4})\.csv$")

    ENGLISH_HEADER = "filiale;"

    PRICE_MAP = {
        "price": ("retail_price", False),
        "unit_price": ("price_per_uom", False),
        "anchor_price": ("anchor_price", False),
    }

    FIELD_MAP = {
        "product": ("article_name", True),
        "product_id": ("code", True),
        "brand": ("brand", False),
        "quantity": ("", False),  # not published, part of the name
        "unit": ("unit_of_measure", False),
        "barcode": ("barcode", False),
        "category": ("", False),
        "special_sale_type": ("promo_name", False),
        "anchor_price_date": ("anchor_date", False),
    }

    # promo_active is DA/NE. There is no separate special price.
    BOOL_MAP = {
        "on_special": ("promo_active", False),
        "available": ("availability", False),
    }

    # Without promo_active, sale prices would pass as regular prices.
    REQUIRED_COLUMNS = [
        "code",
        "article_name",
        "brand",
        "unit_of_measure",
        "retail_price",
        "promo_active",
        "anchor_price",
        "barcode",
    ]

    # Rulebook 1584 (NN 132/2026) requires neither availability nor the sale
    # name, and the unit price only "ako je primjenjivo".
    OPTIONAL_COLUMNS = [
        "price_per_uom",
        "promo_name",
        "anchor_date",
        "availability",
    ]

    # The "Sidrena cijena" header states no date; each row has one in anchor_date
    ANCHOR_PRICE_DATE = None

    # Beeline jewellery is listed in every store but was never available
    # (2026-11-17 to 11-25), and is half of all rows.
    NEVER_AVAILABLE_BRAND = "BEELINE"

    def __init__(self):
        super().__init__()
        self._skipped_zero_price = 0
        self._skipped_never_available = 0
        self._kept_never_available = 0

    def get_store_urls(self, date: datetime.date) -> dict[str, str]:
        """
        Find the CSV of each store for the date on the index page.

        Args:
            date: The date of the price lists

        Returns:
            CSV URL by store code; empty if the date has no price lists
        """
        content = self.fetch_text(self.INDEX_URL)
        day_path = self.DAY_PATH.format(date=date)

        latest: dict[str, tuple[tuple[str, int], str]] = {}
        soup = BeautifulSoup(content, "html.parser")
        for link in soup.select('a[href$=".csv"]'):
            url = urljoin(self.INDEX_URL, str(link["href"]))
            if day_path not in url:
                continue

            filename = unquote(url.rsplit("/", 1)[-1])
            m = self.FILE_PATTERN.search(filename)
            if not m:
                logger.warning(f"Skipping {filename}: unexpected file name")
                continue

            store_id, number, time = m.groups()
            version = (time, int(number))

            # A changed price list for the same day replaces the earlier one
            if store_id in latest:
                logger.info(f"Bipa {store_id} has more than one file for {date}")
                if version < latest[store_id][0]:
                    continue
            latest[store_id] = (version, url)

        return {store_id: url for store_id, (_, url) in latest.items()}

    def read_csv(self, text: str, delimiter: str = ",") -> DictReader:
        # Start at the English header. Without one, the header check in
        # parse_csv reports the missing columns.
        lines = text.splitlines()
        start = next(
            (i for i, line in enumerate(lines) if line.startswith(self.ENGLISH_HEADER)),
            0,
        )
        return DictReader(lines[start:], delimiter=delimiter)

    def keep_row(self, row: dict[str, str]) -> bool:
        # Testers, coupons, free items
        if self.parse_price(row.get("retail_price"), required=False) == 0:
            self._skipped_zero_price += 1
            return False

        if (row.get("brand") or "").strip() == self.NEVER_AVAILABLE_BRAND:
            if self.parse_bool(row.get("availability")) is False:
                self._skipped_never_available += 1
                return False
            self._kept_never_available += 1

        return True

    def fix_product_data(self, data: dict) -> dict:
        on_special = data.pop("on_special", None)
        data = super().fix_product_data(data)

        # One price column plus a DA/NE flag: on sale rows the published price
        # is the sale price, and the regular price is not published.
        if on_special:
            data["special_price"] = data["price"]

        return data

    def get_all_products(self, date: datetime.date) -> list[Store]:
        store_urls = self.get_store_urls(date)
        if not store_urls:
            logger.info(f"No Bipa price list for {date}")
            return []

        stores = []
        stores_never_available = 0
        for store_id, url in sorted(store_urls.items()):
            kept_before = self._kept_never_available
            try:
                content = self.fetch_text(url, encodings=["utf-8", "iso-8859-2"])
                products = self.parse_csv(content, delimiter=";")
            except CrawlerBlocked:
                raise
            except Exception as e:
                logger.error(f"Error processing {url}: {e}", exc_info=True)
                continue

            if self._kept_never_available > kept_before:
                stores_never_available += 1

            store = self.get_mapped_store(store_id, f"Bipa {store_id}")
            store.items = products
            stores.append(store)

        logger.info(
            f"Bipa: skipped {self._skipped_zero_price} rows with price 0 "
            f"(testers, coupons, free items) and {self._skipped_never_available} "
            f"unavailable {self.NEVER_AVAILABLE_BRAND} rows"
        )
        if self._kept_never_available:
            logger.warning(
                f"Bipa: {self._kept_never_available} {self.NEVER_AVAILABLE_BRAND} "
                f"row(s) in {stores_never_available} store(s) are not marked "
                f"unavailable and were kept. The crawler skips unavailable "
                f"{self.NEVER_AVAILABLE_BRAND} rows because the brand was never "
                "available; check whether that still holds."
            )

        return stores


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    crawler = BipaCrawler()
    stores = crawler.get_all_products(datetime.date.today())
    print(stores[0])
    print(stores[0].items[0])
