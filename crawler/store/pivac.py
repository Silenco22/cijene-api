import datetime
import logging
import re
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from crawler.store.models import Store

from .base import BaseCrawler

logger = logging.getLogger(__name__)


class PivacCrawler(BaseCrawler):
    """
    Crawler for Pivac (butcher shops and markets).

    Each day has a page at /hr/cjenici/YYYY-MM-DD, which links one ZIP with a
    CSV per store. The file names carry the store's city and street without
    diacritics and without a separator between them, so the store details
    come from enrichment/stores.csv, keyed by the store code in the file name.
    """

    CHAIN = "pivac"
    BASE_URL = "https://pivac.hr"
    DAY_URL = BASE_URL + "/hr/cjenici/{date:%Y-%m-%d}"

    # 28128148322_Mesnica_Vrgorac_Hrvatskih_Velikana_49_T001_roba_1_20261125_0800.csv
    # "roba" is the goods list; a services list ("usluge") would not match.
    FILE_PATTERN = re.compile(r"_(T\d{3})_roba_\d+_(\d{8})_\d{4}\.csv$")

    PRICE_MAP = {
        "price": ("MALOPRODAJNA_CIJENA", False),
        "unit_price": ("CIJENA_ZA_JEDINICU_MJERE", False),
        "anchor_price": ("BAZNA_CIJENA", False),
    }

    # EAN_BARKOD is empty in every row, BARKOD holds the barcodes.
    FIELD_MAP = {
        "product": ("NAZIV", True),
        "product_id": ("ŠIFRA", True),
        "brand": ("MARKA", False),
        "quantity": ("NETO_KOLIČINA", False),
        "unit": ("JEDINICA_MJERE", False),
        "barcode": ("BARKOD", False),
        "category": ("", False),
    }

    # DA/NE. There is no separate special price and no name for the sale.
    BOOL_MAP = {
        "on_special": ("POSEBAN_OBLIK_PRODAJE", False),
    }

    # Without the flag, sale prices would pass as regular prices.
    REQUIRED_COLUMNS = [
        "NAZIV",
        "ŠIFRA",
        "MARKA",
        "JEDINICA_MJERE",
        "MALOPRODAJNA_CIJENA",
        "POSEBAN_OBLIK_PRODAJE",
        "BARKOD",
        "BAZNA_CIJENA",
    ]

    OPTIONAL_COLUMNS = [
        "NETO_KOLIČINA",
        "CIJENA_ZA_JEDINICU_MJERE",
    ]

    # The "BAZNA_CIJENA" header states no date
    ANCHOR_PRICE_DATE = None

    def get_zip_url(self, date: datetime.date) -> str | None:
        """
        Find the ZIP for the date on its day page.

        Args:
            date: The date of the price list

        Returns:
            URL of the ZIP, or None if there is no price list for the date

        Raises:
            ValueError: The day page has no ZIP link.
        """
        url = self.DAY_URL.format(date=date)
        try:
            content = self.fetch_text(url)
        except httpx.HTTPStatusError as err:
            if err.response.status_code == 404:
                return None
            raise

        link = BeautifulSoup(content, "html.parser").select_one(
            "a.pricelist-day__zip[href]"
        )
        if link is None:
            raise ValueError(f"No ZIP link on {url}")

        return urljoin(url, str(link["href"]))

    def get_all_products(self, date: datetime.date) -> list[Store]:
        zip_url = self.get_zip_url(date)
        if zip_url is None:
            logger.info(f"No Pivac price list for {date}")
            return []

        stores = []
        expected_date = f"{date:%Y%m%d}"
        for filename, content in self.get_zip_contents(zip_url, ".csv"):
            m = self.FILE_PATTERN.search(filename)
            if not m:
                logger.warning(f"Skipping {filename}: unexpected file name")
                continue

            store_id, file_date = m.groups()
            if file_date != expected_date:
                logger.warning(f"Skipping {filename}: not for {date}")
                continue

            try:
                products = self.parse_csv(content.decode("utf-8"), delimiter=";")
            except Exception as e:
                logger.error(f"Error processing {filename}: {e}", exc_info=True)
                continue

            store = self.get_mapped_store(store_id, f"Pivac {store_id}")
            store.items = products
            stores.append(store)

        return stores

    def fix_product_data(self, data: dict) -> dict:
        on_special = data.pop("on_special", None)
        data = super().fix_product_data(data)

        # One price column plus a DA/NE flag: on sale rows the published price
        # is the sale price, and the regular price is not published.
        if on_special:
            data["special_price"] = data["price"]

        return data


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    crawler = PivacCrawler()
    stores = crawler.get_all_products(datetime.date.today())
    print(stores[0])
    print(stores[0].items[0])
