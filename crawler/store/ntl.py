import datetime
import logging
import os
import re
from typing import Iterator
from urllib.parse import unquote

from bs4 import BeautifulSoup

from crawler.store.models import Product, Store

from .base import BaseCrawler

logger = logging.getLogger(__name__)


class NtlCrawler(BaseCrawler):
    """Crawler for NTL store prices."""

    CHAIN = "ntl"
    BASE_URL = "https://ntl.hr/cjenik/"

    # Store information and publication date from the filename, with the
    # strptime format of the date. The street address can itself contain
    # underscores (e.g. "Calinec 179_b", where the source has a slash), so the
    # patterns are anchored on the fixed-format tail and the address takes
    # whatever is left. The index serves the last 30 days, so both formats
    # stay in use for a while.
    FILENAME_PATTERNS = [
        # Since 2026-11-24:
        # 78344221376_Supermarket_Frana Galovica 11A_KRAPINA_10101_cjenik_robe_31601_20261126_0202.csv
        (
            re.compile(
                r"^\d+_(?P<store_type>[^_]+)_(?P<street_address>.+)_(?P<city>[^_]+)_"
                r"(?P<store_id>\d+)_cjenik_robe_\d+_(?P<date>\d{8})_\d{4}\.csv$",
                re.IGNORECASE,
            ),
            "%Y%m%d",
        ),
        # Until 2026-11-23:
        # Supermarket_Ljudevita Gaja 1_DUGA RESA_10103_263_25052025_07_22_36.csv
        (
            re.compile(
                r"^(?P<store_type>[^_]+)_(?P<street_address>.+)_(?P<city>[^_]+)_"
                r"(?P<store_id>\d+)_\d+_(?P<date>\d{8})_\d{2}_\d{2}_\d{2}\.csv$",
                re.IGNORECASE,
            ),
            "%d%m%Y",
        ),
    ]

    # The index keeps the last 30 days, as the page itself states.
    RETENTION_DAYS = 30

    # Mapping for price fields from CSV columns
    PRICE_MAP = {
        # field: (column_name, is_required)
        "price": ("Maloprodajna cijena", False),
        "unit_price": ("Cijena za jedinicu mjere", False),
        "special_price": ("MPC za vrijeme posebnog oblika prodaje", False),
        # Renamed on 2026-09-25 when the chain moved to the NN 101/2026 format.
        # NTL dropped the reference date from the header entirely, where
        # Gavranović and Trgovina Krk spell it out; the values themselves are
        # still mostly the 2.5.2025 anchor, so neither spelling tells us which
        # reference date a given row refers to. Renamed to "Bazna cijena" by
        # 2026-11-24 (NN 132/2026).
        "anchor_price": (
            ["Sidrena cijena na 2.5.2025", "Sidrena cijena", "Bazna cijena"],
            False,
        ),
    }

    # Mapping for other product fields from CSV columns
    FIELD_MAP = {
        "product_id": ("Šifra proizvoda", True),
        "barcode": ("Barkod", False),
        "product": ("Naziv proizvoda", True),
        "brand": ("Marka proizvoda", False),
        "quantity": ("Neto količina", False),
        "unit": ("Jedinica mjere", False),
        "category": ("Kategorija proizvoda", False),
        "special_sale_type": ("Naziv posebnog oblika prodaje", False),
    }

    BOOL_MAP = {
        "available": ("Dostupnost", False),
    }

    REQUIRED_COLUMNS = [
        "Maloprodajna cijena",
        "Cijena za jedinicu mjere",
        ["Sidrena cijena na 2.5.2025", "Sidrena cijena", "Bazna cijena"],
        "Šifra proizvoda",
        "Barkod",
        "Naziv proizvoda",
        "Marka proizvoda",
        "Jedinica mjere",
    ]

    # On 2026-09-25 the chain dropped the special price and 30-day low columns
    # and added the sale name and availability ones. Files from either side of
    # the switch must keep parsing, since the index serves the last 30 days and
    # most of those are still in the old format.
    OPTIONAL_COLUMNS = [
        "Neto količina",
        "Kategorija proizvoda",
        "MPC za vrijeme posebnog oblika prodaje",
        "Naziv posebnog oblika prodaje",
        "Dostupnost",
    ]

    def parse_index(self, content: str) -> list[str]:
        """
        Parse the NTL index page to extract CSV links.

        Args:
            content: HTML content of the index page

        Returns:
            List of absolute CSV URLs on the page
        """
        soup = BeautifulSoup(content, "html.parser")
        urls = []

        for link_tag in soup.select('a[href$=".csv"]'):
            href = str(link_tag.get("href"))
            urls.append(href)

        return list(set(urls))  # Return unique URLs

    def match_filename(self, url: str) -> tuple[re.Match[str], str] | None:
        """
        Match a CSV URL's filename against the known formats.

        Returns:
            The match and the strptime format of its date, or None if no
            format matches.
        """
        filename = unquote(os.path.basename(url))
        for pattern, date_format in self.FILENAME_PATTERNS:
            match = pattern.match(filename)
            if match:
                return match, date_format
        return None

    def filename_date(self, url: str) -> datetime.date | None:
        """
        Read the publication date out of a CSV filename, e.g. 2026-11-26 from
        ..._KRAPINA_10101_cjenik_robe_31601_20261126_0202.csv. Returns None if
        the filename doesn't carry a usable date.
        """
        matched = self.match_filename(url)
        if not matched:
            return None

        match, date_format = matched
        try:
            return datetime.datetime.strptime(match["date"], date_format).date()
        except ValueError:
            return None

    def parse_store_info(self, url: str) -> Store:
        """
        Extracts store information from a CSV download URL.

        Example URL:
        https://ntl.hr/uploads/csv/78344221376_Supermarket_Frana%20Galovica%2011A_KRAPINA_10101_cjenik_robe_31601_20261126_0202.csv

        Args:
            url: CSV download URL with store information in the filename

        Returns:
            Store object with parsed store information
        """
        logger.debug(f"Parsing store information from NTL URL: {url}")

        matched = self.match_filename(url)
        if not matched:
            filename = unquote(os.path.basename(url))
            raise ValueError(f"Invalid CSV filename format for NTL: {filename}")

        data = matched[0].groupdict()

        store_type = data["store_type"].lower()
        street_address = data["street_address"]
        city = data["city"].title()
        store_id = data["store_id"]

        store = Store(
            chain=self.CHAIN,
            store_type=store_type,
            store_id=store_id,
            name=f"NTL {city}",
            street_address=street_address,
            zipcode="",  # Zipcode is not available in the filename
            city=city,
            items=[],
        )

        logger.info(
            f"Parsed NTL store: {store.name}, Address: {store.street_address}, City: {store.city}"
        )
        return store

    def get_store_prices(self, csv_url: str) -> list[Product]:
        """
        Fetch and parse store prices from an NTL CSV URL.
        The CSV is semicolon-separated and windows-1250 encoded.

        Args:
            csv_url: URL to the CSV file containing prices

        Returns:
            List of Product objects
        """
        try:
            content = self.fetch_text(csv_url, encodings=["windows-1250"])
            return self.parse_csv(content, delimiter=";")
        except Exception as e:
            logger.error(
                f"Failed to get NTL store prices from {csv_url}: {e}",
                exc_info=True,
            )
            return []

    def get_index(self, date: datetime.date) -> list[str]:
        """
        Fetch and parse the NTL index page to get CSV URLs.

        Args:
            date: The date to fetch CSV files for

        Returns:
            List of CSV URLs available for the given date.
        """
        index_url = f"{self.BASE_URL}?date={date:%Y-%m-%d}"
        logger.info(f"Fetching NTL CSV files for {date:%Y-%m-%d} from {index_url}")

        content = self.fetch_text(index_url)
        if not content:
            logger.warning(f"No content found at NTL index URL: {index_url}")
            return []

        all_urls = self.parse_index(content)
        if not all_urls:
            age = (datetime.date.today() - date).days
            if age > self.RETENTION_DAYS:
                logger.warning(
                    f"No NTL CSV URLs for {date:%Y-%m-%d}: {age} days old, past the "
                    f"{self.RETENTION_DAYS}-day retention window"
                )
            else:
                logger.warning(f"No NTL CSV URLs listed for {date:%Y-%m-%d}")
            return []

        # The date is a query parameter, so confirm the files we got back are the
        # ones we asked for rather than trusting the page. Files whose filename
        # carries no date are kept, since only the date claim is being checked
        # here and parse_store_info rejects unusable names anyway.
        urls = []
        for url in all_urls:
            file_date = self.filename_date(url)
            if file_date is not None and file_date != date:
                logger.warning(
                    f"Ignoring NTL CSV dated {file_date:%Y-%m-%d} listed under "
                    f"{date:%Y-%m-%d}: {url}"
                )
                continue
            urls.append(url)

        logger.info(f"Found {len(urls)} NTL CSV files for {date:%Y-%m-%d}")
        return urls

    def get_all_products(self, date: datetime.date) -> list[Store]:
        return list(self.iter_all_products(date))

    def iter_all_products(self, date: datetime.date) -> Iterator[Store]:
        """
        Main method to fetch and parse all NTL store, product, and price info.

        Fork change (2026-10-08): a generator yielding one store at a time
        (343 stores and 1.5M rows per day since NN 101/2026).

        Args:
            date: The date to fetch data for

        Yields:
            Store objects with their products.
        """
        csv_links = self.get_index(date)

        if not csv_links:
            logger.warning(f"No NTL CSV links found for date {date:%Y-%m-%d}")
            return

        for url in csv_links:
            try:
                store = self.parse_store_info(url)
                products = self.get_store_prices(url)
            except ValueError as ve:
                logger.error(
                    f"Skipping store due to parsing error from URL {url}: {ve}",
                    exc_info=False,
                )
                continue
            except Exception as e:
                logger.error(
                    f"Error processing NTL store from {url}: {e}", exc_info=True
                )
                continue

            if not products:
                logger.warning(f"No products found for NTL store at {url}, skipping.")
                continue

            store.items = products
            yield store

    def fix_product_data(self, data: dict) -> dict:
        """
        Clean and fix NTL-specific product data.

        Args:
            data: Dictionary containing the row data

        Returns:
            The cleaned data
        """
        if "product" in data and data["product"]:
            data["product"] = data["product"].strip()

        # Call parent method for common fixups
        data = super().fix_product_data(data)

        # Since 2026-09-25 the chain publishes one price column plus the name of
        # the special form of sale, having dropped the separate special price
        # column. When a sale name is present the published price is the
        # promotional one, so mirror it into special_price to keep the "on sale"
        # signal. Verified against the last file published in the old format:
        # of 400 rows carrying a sale name (AKC, RAS or TNC), all 400 had a
        # special price the previous day and 399 of them at exactly this price,
        # and none of the 5094 rows without a name did. The regular pre-promo
        # price is no longer published.
        if data.get("special_sale_type") and data.get("special_price") is None:
            data["special_price"] = data.get("price")

        return data


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    crawler = NtlCrawler()
    stores = crawler.crawl(datetime.date.today())
    print(stores[0])
    print(stores[0].items[0])
