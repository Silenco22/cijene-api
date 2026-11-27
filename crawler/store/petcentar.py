import datetime
import json
import logging
import re
from csv import DictReader
from urllib.parse import quote

from bs4 import BeautifulSoup

from crawler.store.models import Store

from .base import BaseCrawler, CrawlerBlocked

logger = logging.getLogger(__name__)


class PetCentarCrawler(BaseCrawler):
    """
    Crawler for Pet Centar (pet shops).

    The price-list page embeds a JSON block with every store and its files
    of the last month, each with the date the page lists it under. A file is
    exported at about 23:40 on the evening before that date and holds the
    prices for that date. Store details come from enrichment/stores.csv,
    keyed by the store code. The web shop has its own price list and is kept
    as a store.

    The CSVs are Windows-1250 and semicolon-separated, with text quoted
    with '. There is no sale price and no sale flag. The anchor price has a
    date per row.
    """

    CHAIN = "petcentar"
    INDEX_URL = "https://www.pet-centar.hr/pages/objava-cjenika"

    # _0050_20261125_234113.csv: export counter, export date and time
    FILE_PATTERN = re.compile(r"^_(\d{4})_(\d{8})_(\d{6})\.csv$")

    # The web shop is not in the page's store list; the page script names it so
    WEBSHOP_NAME = "Pet centar webshop"

    PRICE_MAP = {
        "price": ("mpc", False),
        "unit_price": ("mpcJed", False),
        "anchor_price": ("sidrena_cijena", False),
    }

    FIELD_MAP = {
        "product": ("kratki_naziv", True),
        "product_id": ("pom_sifra", True),
        "brand": ("brand", False),
        "quantity": ("", False),  # not published, part of the name
        "unit": ("jedmjereoznaka", False),
        "barcode": ("sifra", False),
        "category": ("naziv", False),  # category name; the product name is kratki_naziv
        "anchor_price_date": ("sidreni_datum", False),
    }

    REQUIRED_COLUMNS = [
        "kratki_naziv",
        "pom_sifra",
        "brand",
        "jedmjereoznaka",
        "mpc",
        "sidrena_cijena",
        "sifra",
    ]

    # The unit price is required only "ako je primjenjivo" (rulebook 1584)
    OPTIONAL_COLUMNS = [
        "mpcJed",
        "naziv",
        "sidreni_datum",
    ]

    # "sidrena_cijena" states no date; each row has one in sidreni_datum
    ANCHOR_PRICE_DATE = None

    def __init__(self):
        super().__init__()
        self._skipped_no_price = 0
        self._skipped_test = 0

    def get_price_lists(self, date: datetime.date) -> dict[str, tuple[str, str]]:
        """
        Find each store's file for the date in the JSON on the price-list page.

        Args:
            date: The date of the price lists

        Returns:
            Store name and file URL by store code; empty if the date has no
            price lists
        """
        soup = BeautifulSoup(self.fetch_text(self.INDEX_URL), "html.parser")
        # The id ends in a Shopify section id, which can change
        script = soup.select_one('script[id^="StorePriceLists-"]')
        if script is None:
            raise ValueError("Price list JSON not found on the page")
        data = json.loads(script.get_text())

        names = {s["code"].strip(): s["name"].strip() for s in data.get("stores", [])}
        price_lists = data["priceLists"]
        day = date.isoformat()

        found = {}
        for code, entry in price_lists["stores"].items():
            latest = None
            for filename, file_day in entry["f"]:
                if file_day != day:
                    continue

                m = self.FILE_PATTERN.match(filename)
                if not m:
                    logger.warning(f"Skipping {filename}: unexpected file name")
                    continue

                number, export_day, export_time = m.groups()
                version = (export_day, export_time, number)

                # A later export for the same day replaces the earlier one
                if latest is not None:
                    logger.info(f"Pet Centar {code} has more than one file for {date}")
                    if version < latest[0]:
                        continue
                url = price_lists["base"] + quote(entry["p"] + filename, safe="")
                latest = (version, url)

            if latest is None:
                continue

            name = names.get(code)
            if not name and code.endswith("_WEB"):
                name = self.WEBSHOP_NAME
            found[code] = (name or f"Pet centar {code}", latest[1])

        missing = sorted(set(price_lists["stores"]) - set(found))
        if found and missing:
            logger.info(f"Pet Centar: no price list for {date} in {', '.join(missing)}")
        return found

    def read_csv(self, text: str, delimiter: str = ",") -> DictReader:
        # Text fields are quoted with ' and a ' inside is doubled ('HILL''s')
        return DictReader(text.splitlines(), delimiter=delimiter, quotechar="'")

    def keep_row(self, row: dict[str, str]) -> bool:
        # No price at all: price, unit price and anchor price are all empty
        if not self.parse_price(row.get("mpc"), required=False):
            self._skipped_no_price += 1
            return False

        # Test rows: "test definicije", "test kalkulacija%"
        if (row.get("kratki_naziv") or "").strip().startswith("test"):
            self._skipped_test += 1
            return False

        return True

    def get_all_products(self, date: datetime.date) -> list[Store]:
        price_lists = self.get_price_lists(date)
        if not price_lists:
            logger.info(f"No Pet Centar price list for {date}")
            return []

        stores = []
        for code, (name, url) in sorted(price_lists.items()):
            try:
                # Rulebook 1584 requires UTF-8; the files so far are Windows-1250
                content = self.fetch_text(url, encodings=["utf-8", "windows-1250"])
                products = self.parse_csv(content, delimiter=";")
            except CrawlerBlocked:
                raise
            except Exception as e:
                logger.error(f"Error processing Pet Centar {code}: {e}", exc_info=True)
                continue

            store = self.get_mapped_store(code, name)
            store.items = products
            stores.append(store)

        logger.info(
            f"Pet Centar: skipped {self._skipped_no_price} rows without a price "
            f"and {self._skipped_test} test rows"
        )
        return stores


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    crawler = PetCentarCrawler()
    stores = crawler.get_all_products(datetime.date.today())
    print(stores[0])
    print(stores[0].items[0])
