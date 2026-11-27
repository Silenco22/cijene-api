from __future__ import annotations

import datetime
import logging
import re
from typing import NamedTuple
from urllib.parse import unquote, urlparse

from bs4 import BeautifulSoup

from crawler.store.base import BaseCrawler
from crawler.store.models import Product, Store

logger = logging.getLogger(__name__)


class RotoCrawler(BaseCrawler):
    """
    Crawler for Roto store prices.
    https://www.rotodinamic.hr/cjenici/
    """

    CHAIN = "roto"
    BASE_URL = "https://www.rotodinamic.hr"
    INDEX_URL = f"{BASE_URL}/cjenici/"

    # Store ID, publication date and time at the end of a CSV filename
    FILENAME_PATTERN = re.compile(
        r"_(?P<store_id>D\d+)_roba_\d+_(?P<date>\d{8})_(?P<time>\d{4})\.csv$"
    )

    ANCHOR_PRICE_COLUMN = "sidrena cijena na 2.5.2025."
    ANCHOR_PRICE_DATE = datetime.date(2025, 5, 2)

    # Roto also publishes the price on 10.9.2026. It is used only for products
    # without a 2.5.2025 price, i.e. those listed after that date.
    NEW_ANCHOR_PRICE_COLUMN = "sidrena cijena na 10.9.2026."
    NEW_ANCHOR_PRICE_DATE = datetime.date(2026, 9, 10)

    # Since 2026-11-19 both anchor columns are replaced by "Bazna cijena"
    # (NN 132/2026). Its values match the 2.5.2025 anchor prices, but the
    # header states no date and the reference date differs per product, so
    # these rows get no date.
    BASE_PRICE_COLUMN = "Bazna cijena"

    PRICE_MAP = {
        "price": ("MPC", True),
        "unit_price": ("Cijena za jedinicu mjere", True),
        "special_price": ("MPC za vrijeme posebnog oblika prodaje", False),
        "best_price_30": ("Najniža cijena u posljednjih 30 dana", False),
        "anchor_price": (ANCHOR_PRICE_COLUMN, False),
        "new_anchor_price": (NEW_ANCHOR_PRICE_COLUMN, False),
        "base_price": (BASE_PRICE_COLUMN, False),
    }

    FIELD_MAP = {
        "product": ("Naziv artikla", False),
        "product_id": ("ŠIFRA ARTIKLA", True),
        "brand": ("BRAND", False),
        "quantity": ("neto količina", False),
        "unit": ("Jedinica mjere", False),
        "barcode": ("Barkod", False),
        "category": ("Kategorija proizvoda", False),
    }

    REQUIRED_COLUMNS = [
        "MPC",
        "Cijena za jedinicu mjere",
        (ANCHOR_PRICE_COLUMN, BASE_PRICE_COLUMN),
        "Naziv artikla",
        "ŠIFRA ARTIKLA",
        "BRAND",
        "Jedinica mjere",
        "Barkod",
    ]

    # Not required by NN 101/2026, so the chain may drop them when it switches.
    OPTIONAL_COLUMNS = [
        "neto količina",
        "Kategorija proizvoda",
        "MPC za vrijeme posebnog oblika prodaje",
        "Najniža cijena u posljednjih 30 dana",
        "sidrena cijena na 10.9.2026.",
    ]

    def fix_product_data(self, data: dict) -> dict:
        """
        Fall back to the 10.9.2026 anchor price for newer products, and read
        "Bazna cijena" as the anchor price in files that have it.
        """
        new_anchor_price = data.pop("new_anchor_price", None)
        base_price = data.pop("base_price", None)
        data = super().fix_product_data(data)

        if data.get("anchor_price") is None and new_anchor_price is not None:
            data["anchor_price"] = new_anchor_price
            data["anchor_price_date"] = self.NEW_ANCHOR_PRICE_DATE.isoformat()

        # A base price of 0 means none is published, as for the other prices
        if data.get("anchor_price") is None and base_price:
            data["anchor_price"] = base_price
            data["anchor_price_date"] = None

        return data

    def get_csv_urls(
        self, soup: BeautifulSoup, date: datetime.date
    ) -> list[tuple[str, str, str]]:
        """
        Find the price lists published on the date, the latest one per store.

        Since 2026-11-26 Roto publishes one CSV per store, named like
        24723122482_Cash&Carry_Kovinska 1 ZAGREB_D01_roba_382_20261126_0752.csv.
        The link text holds the store ID and name ("D01 Zagreb"), and the name
        is the key of the store's address on the same page.

        Returns:
            (CSV URL, store ID, store name) for each store.
        """
        target = f"{date:%Y%m%d}"
        latest: dict[str, tuple[str, str, str]] = {}

        for anchor in soup.select("a.cjenici-table-row"):
            url = anchor.attrs["href"]
            assert isinstance(url, str)
            match = self.FILENAME_PATTERN.search(unquote(urlparse(url).path))
            if not match or match["date"] != target:
                continue

            store_id = match["store_id"]
            label = anchor.select_one(".cjenici-datum")
            label_text = label.get_text(" ", strip=True) if label else ""
            name = label_text.removeprefix(store_id).strip()

            time = match["time"]
            if store_id not in latest or time > latest[store_id][0]:
                latest[store_id] = (time, url, name)

        if not latest:
            raise ValueError(f"No price list found for {date}")

        return [(url, store_id, name) for store_id, (_, url, name) in latest.items()]

    def get_all_products(self, date: datetime.date) -> list[Store]:
        html_content = self.fetch_text(self.INDEX_URL)
        soup = BeautifulSoup(html_content, "html.parser")
        csv_files = self.get_csv_urls(soup, date)
        addresses = self.parse_store_addresses(soup)

        stores = []
        for csv_url, store_id, name in csv_files:
            products = self.get_store_products(csv_url)
            if not products:
                logger.warning(f"No products for {store_id} {name}, skipping")
                continue
            stores.append(self.get_store(store_id, name, products, addresses))
        return stores

    def get_store_products(self, csv_url: str) -> list[Product]:
        try:
            content = self.fetch_text(csv_url, encodings=["utf-8-sig", "cp1250"])
            return self.parse_csv(content, delimiter=";")
        except Exception:
            logger.exception(f"Failed to get store prices from {csv_url}")
            return []

    def get_store(
        self,
        store_id: str,
        name: str,
        products: list[Product],
        addresses: dict[str, Address],
    ) -> Store:
        if name in addresses:
            street_address, zipcode, city = addresses[name]
        else:
            street_address, zipcode, city = "", "", ""
            logger.warning(f"Unable to find address for {store_id} {name}")

        return Store(
            chain=self.CHAIN,
            store_type="Cash & Carry",
            store_id=store_id,
            name=f"Cash & Carry {name}",
            street_address=street_address,
            zipcode=zipcode,
            city=city,
            items=products,
        )

    def parse_store_addresses(self, soup: BeautifulSoup) -> dict[str, Address]:
        """Returns store address indexed by store name"""
        addresses = {}

        spans = soup.select(".container > div.mBottom50 > p > span.bold")
        for span in spans:
            name = span.text
            assert span.parent is not None
            _, address = span.parent
            assert isinstance(address, str)
            street_address, zipcode_city = address.strip(" -").split(", ")
            zipcode, city = zipcode_city.split(" ", maxsplit=1)

            # Remove unwanted address prefix
            to_strip = "Jankomir- "
            if street_address.startswith(to_strip):
                street_address = street_address[len(to_strip) :]

            if name in addresses:
                logger.warning(f"Duplicate store: {name}")

            addresses[name] = Address(street_address, zipcode, city)

        return addresses


class Address(NamedTuple):
    street_address: str
    zipcode: str
    city: str


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    crawler = RotoCrawler()
    # The index lists only the current day's price lists
    stores = crawler.get_all_products(datetime.date.today())
    from pprint import pp

    pp(stores[0])
