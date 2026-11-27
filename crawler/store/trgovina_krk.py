import datetime
import logging
import re
from typing import Any, Dict, List, Optional
from urllib.parse import unquote

from bs4 import BeautifulSoup

from crawler.store.models import Store

from .base import BaseCrawler

logger = logging.getLogger(__name__)


class TrgovinaKrkCrawler(BaseCrawler):
    """
    Crawler for Trgovina Krk chain stores.

    Retrieves price data from daily CSV files published on their website.
    Each store location has separate CSV files updated daily.
    """

    CHAIN = "trgovina-krk"
    BASE_URL = "https://trgovina-krk.hr"
    INDEX_URL = "https://trgovina-krk.hr/objava-cjenika/"

    # Since 2026-11-11 the index lists all ~780 stores of the chain, not only
    # the supermarkets. Only these store types are crawled.
    STORE_TYPES = {"Supermarket", "Minimarket", "Klasicna prodavaonica"}

    # Left out on purpose: specialized stores (some sell building materials)
    # and gas station shops. Any other type gets a warning.
    SKIPPED_STORE_TYPES = {
        "Specijalizirana prodavaonica",
        "Benzinska postaja i prodavaonica",
    }

    # "[OIB_]<type>_<address>_<CITY>_<code>_[cjenik_robe_]<number>_<time>.csv",
    # the OIB and "cjenik_robe" since 2026-11-20. "_" in the address stands
    # for a space or a removed character ("Trg sv. Jurja 11_A").
    FILE_NAME_RE = re.compile(
        r"^(?:\d{11}_)?(?P<type>[^_]+)_(?P<address>.+)_(?P<city>[^_]+)"
        r"_(?P<code>\d{6})_(?:cjenik_robe_)?\d+_\d"
    )

    # Mapping for price fields
    PRICE_MAP = {
        "price": ("Maloprodajna cijena", True),
        "unit_price": ("Cijena za jedinicu mjere", False),
        "special_price": ("MPC za vrijeme posebnog oblika prodaje", False),
        "best_price_30": ("Najniža cijena u poslj.30 dana", False),
        # Renamed on 2026-09-23 when the chain moved to the NN 101/2026 format,
        # and again by 2026-09-30 to plain "Sidrena cijena" (as NTL did).
        # The values did not change with the header: on the switch day nearly
        # every pre-existing product kept its 2.5.2025 value, so the column name
        # says nothing reliable about which reference date a row refers to.
        # Renamed to "Bazna cijena" by 2026-11-25 (NN 132/2026).
        "anchor_price": (
            [
                "Sidrena cijena na 2.5.2025",
                "Sidrena cijena na 10.09.2026",
                "Sidrena cijena",
                "Bazna cijena",
            ],
            False,
        ),
    }

    # Mapping for other fields
    FIELD_MAP = {
        "product": ("Naziv proizvoda", True),
        "product_id": ("Šifra proizvoda", True),
        "brand": ("Marka proizvoda", False),
        "quantity": ("Neto količina", False),
        "unit": ("Jedinica mjere", False),
        "barcode": ("Barkod", False),
        "category": ("Kategorija proizvoda", False),
        "special_sale_type": ("Naziv posebnog oblika prodaje", False),
    }

    BOOL_MAP = {
        "available": ("Dostupnost", False),
    }

    REQUIRED_COLUMNS = [
        "Maloprodajna cijena",
        "Cijena za jedinicu mjere",
        [
            "Sidrena cijena na 2.5.2025",
            "Sidrena cijena na 10.09.2026",
            "Sidrena cijena",
            "Bazna cijena",
        ],
        "Naziv proizvoda",
        "Šifra proizvoda",
        "Marka proizvoda",
        "Jedinica mjere",
        "Barkod",
    ]

    # On 2026-09-24 the chain dropped the special price and 30-day low columns
    # and added the sale name and availability ones. Files from either side of
    # the switch must keep parsing.
    OPTIONAL_COLUMNS = [
        "Neto količina",
        "Kategorija proizvoda",
        "MPC za vrijeme posebnog oblika prodaje",
        "Najniža cijena u poslj.30 dana",
        "Naziv posebnog oblika prodaje",
        "Dostupnost",
    ]

    def get_all_products(self, date: datetime.date) -> List[Store]:
        """Get all products from all store locations."""
        try:
            # Get the index page
            content = self.fetch_text(self.INDEX_URL)
            soup = BeautifulSoup(content, "html.parser")

            stores = []

            for store, csv_url in self._parse_index(soup, date):
                logger.info(f"Processing store {store.store_id}: {store.name}")

                # Download and process CSV
                store.items = self._process_csv_file(csv_url)
                stores.append(store)

                logger.info(
                    f"Retrieved {len(store.items)} products from {store.store_id}"
                )

            return stores

        except Exception as e:
            logger.error(f"Error getting products: {str(e)}")
            raise

    def _parse_index(
        self, soup: BeautifulSoup, date: datetime.date
    ) -> List[tuple[Store, str]]:
        """
        Find the stores with a price list for the date.

        The index page groups the links by store, newest first, as
        "DD.MM.YYYY. - filename.csv". A few groups hold several stores, so the
        store comes from the file name. The first link of a store for the date
        is its latest file.

        Returns:
            (store without items, CSV URL) pairs
        """
        hr_date = f"{date:%d.%m.%Y}"
        stores: Dict[str, tuple[Store, str]] = {}
        unknown_types = set()

        for link in soup.select('a[href$=".csv"]'):
            if self._extract_date_from_link(link.get_text()) != hr_date:
                continue

            # The URLs are already absolute.
            csv_url = str(link["href"])
            file_name = unquote(csv_url.rsplit("/", 1)[-1])
            match = self.FILE_NAME_RE.match(file_name)
            if not match:
                logger.warning(f"Unexpected price list file name: {file_name}")
                continue

            store_type = match["type"]
            if store_type not in self.STORE_TYPES:
                if store_type not in self.SKIPPED_STORE_TYPES:
                    unknown_types.add(store_type)
                continue

            code = match["code"]
            if code in stores:
                continue

            city = match["city"].title()
            stores[code] = (
                Store(
                    chain=self.CHAIN,
                    store_id=code,
                    name=f"Trgovina Krk {city}",
                    store_type=store_type.lower(),
                    city=city,
                    street_address=" ".join(match["address"].replace("_", " ").split()),
                    items=[],
                ),
                csv_url,
            )

        if unknown_types:
            logger.warning(f"Skipped unknown store types: {sorted(unknown_types)}")

        logger.info(f"Found {len(stores)} store price lists for {date}")
        return list(stores.values())

    def _extract_date_from_link(self, link_text: str) -> Optional[str]:
        """Extract date from CSV link text."""
        # Format: "05.07.2025 – filename.csv"
        date_match = re.match(r"(\d{2}\.\d{2}\.\d{4})", link_text.strip())
        return date_match.group(1) if date_match else None

    def _process_csv_file(self, csv_url: str) -> List:
        """Download and process a CSV file."""
        try:
            # Download CSV file with Windows-1250 encoding
            content = self.fetch_text(csv_url, encodings=["windows-1250"])

            # Parse CSV
            products = self.parse_csv(content, delimiter=";")

            return products

        except Exception as e:
            logger.error(f"Error processing CSV file {csv_url}: {e}", exc_info=True)
            return []

    def fix_product_data(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Clean up product data, including collapsing multiple spaces."""
        # Call parent method first
        data = super().fix_product_data(data)

        # Collapse multiple spaces in product name
        if data.get("product"):
            data["product"] = re.sub(r"\s+", " ", data["product"].strip())

        # Since 2026-09-24 the chain publishes one price column plus the name of
        # the special form of sale, having dropped the separate special price
        # column. When a sale name is present the published price is the
        # promotional one, so mirror it into special_price to keep the "on sale"
        # signal. Verified against the last file published in the old format:
        # for every sampled promo row the new price equals the old special
        # price. The regular pre-promo price is no longer published.
        if data.get("special_sale_type") and data.get("special_price") is None:
            data["special_price"] = data.get("price")

        return data


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    crawler = TrgovinaKrkCrawler()
    stores = crawler.crawl(datetime.date.today())
    print(stores[0])
    print(stores[0].items[0])
