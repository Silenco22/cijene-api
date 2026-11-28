import datetime
import logging
import re
from typing import Generator, NamedTuple, Optional
from zipfile import ZipFile, ZipInfo

from .base import BaseCrawler
from crawler.store.models import Store

logger = logging.getLogger(__name__)


class StoreFile(NamedTuple):
    """A store's CSV file in the ZIP, and the store as its name describes it."""

    published: datetime.datetime
    info: ZipInfo
    store: Store  # without products


# Wrong postcodes in Plodine's file names: (store_id, published) -> correct.
# Keyed on the published value too, so a fix stops applying once Plodine
# corrects the file name.
ZIPCODE_FIXES = {
    ("010", "51470"): "52470",  # Umag; wrong since early 2026 (#118)
}


class PlodineCrawler(BaseCrawler):
    """
    Crawler for Plodine store prices.

    This class handles downloading and parsing price data from Plodine's website.
    It fetches the price list index page, finds the ZIP for the specified date,
    downloads and extracts it, and parses the CSV files inside.

    Since 2026-10-02 the daily ZIP also carries the previous day's CSVs,
    including intra-day updates, so a store can have several files in one ZIP.
    Only files published on the requested date are used: per store, the
    newest one that parses.
    """

    CHAIN = "plodine"
    BASE_URL = "https://www.plodine.hr"
    INDEX_URL = f"{BASE_URL}/info-o-cijenama"
    ZIP_DATE_PATTERN = re.compile(r".*/cjenici/cjenici_(\d{2})_(\d{2})_(\d{4})_.*\.zip")
    # Publication timestamp (ddmmyyyyHHMMSS) at the end of a CSV filename
    FILE_TIMESTAMP_PATTERN = re.compile(r"_(\d{14})\.csv$")
    VERIFY_TLS_CERT = False  # Plodine uses a root CA unsupported by httpx on Debian 12

    PRICE_MAP = {
        "price": (["Maloprodajna cijena", "MPC"], False),
        "unit_price": ("Cijena po JM", False),
        "special_price": (
            "MPC za vrijeme posebnog oblika prodaje",
            False,
        ),
        "best_price_30": ("Najniza cijena u poslj. 30 dana", False),
        # Renamed to "Bazna cijena" by 2026-11-24 (NN 132/2026)
        "anchor_price": (
            ["Sidrena cijena na 2.5.2025", "Sidrena cijena", "Bazna cijena"],
            False,
        ),
    }

    # The new "Poseban oblik prodaje" column is a DA/NE flag, not a special price column.
    FIELD_MAP = {
        "product": ("Naziv proizvoda", True),
        "product_id": ("Sifra proizvoda", True),
        "brand": ("Marka proizvoda", False),
        "quantity": ("Neto kolicina", False),
        "unit": ("Jedinica mjere", False),
        "barcode": ("Barkod", False),
        "category": ("Kategorija proizvoda", False),
        "special_sale_type": ("Naziv posebnog oblika prodaje", False),
    }

    BOOL_MAP = {
        "available": ("Dostupno nedostupno", False),
    }

    REQUIRED_COLUMNS = [
        ["Maloprodajna cijena", "MPC"],
        "Cijena po JM",
        ["Sidrena cijena na 2.5.2025", "Sidrena cijena", "Bazna cijena"],
        "Naziv proizvoda",
        "Sifra proizvoda",
        "Marka proizvoda",
        "Jedinica mjere",
        "Barkod",
    ]

    OPTIONAL_COLUMNS = [
        "Neto kolicina",
        "Kategorija proizvoda",
        "MPC za vrijeme posebnog oblika prodaje",
        "Najniza cijena u poslj. 30 dana",
        "Naziv posebnog oblika prodaje",
        "Dostupno nedostupno",
    ]

    def get_index(self, date: datetime.date) -> str:
        content = self.fetch_text(self.INDEX_URL)
        zip_urls_by_date = self.parse_index_for_zip(content)
        others = ", ".join(f"{d:%Y-%m-%d}" for d in zip_urls_by_date)
        logger.debug(f"Available price lists: {others}")
        if date not in zip_urls_by_date:
            raise ValueError(f"No price list found for {date}")
        return zip_urls_by_date[date]

    def parse_store_from_filename(self, filename: str) -> Optional[Store]:
        """
        Extract store information from CSV filename using regex.

        Example filename format:
            SUPERMARKET_SJEVERNA_VEZNA_CESTA_31_35000_SLAVONSKI_BROD_022_6_20052025014212.csv
            SUPERMARKET_ULICA_FRANJE_TUDJMANA_83A_10450_JASTREBARSKO_063_2_16052025020937.csv

        Args:
            filename: Name of the CSV file with store information

        Returns:
            Store object with parsed store information, or None if parsing fails
        """
        logger.debug(f"Parsing store information from filename: {filename}")

        try:
            pattern = (
                r"^(SUPERMARKET|HIPERMARKET)_(.+?)_(\d{5})_(.+)_(\d+)_\d+_\d+.*\.csv$"
            )
            match = re.match(pattern, filename)

            if not match:
                logger.warning(f"Failed to match filename pattern: {filename}")
                return None

            store_type, street_address, zipcode, city, store_id = match.groups()

            city = city.replace("_", " ").title()
            zipcode = ZIPCODE_FIXES.get((store_id, zipcode), zipcode)

            store = Store(
                chain="plodine",
                store_id=store_id,
                name=f"Plodine {city}",
                store_type=store_type.lower(),
                city=city,
                street_address=street_address.replace("_", " ").title(),
                zipcode=zipcode,
                items=[],
            )

            logger.info(
                f"Parsed store: {store.name} ({store.store_id}), {store.store_type}, {store.city}, {store.street_address}, {store.zipcode}"
            )
            return store

        except Exception as e:
            logger.error(f"Failed to parse store from filename {filename}: {str(e)}")
            return None

    def parse_file_timestamp(self, filename: str) -> Optional[datetime.datetime]:
        """
        Extract the publication timestamp from a CSV filename.

        Example: ..._VISKOVO_001_509_02102026035529.csv -> 2026-10-02 03:55:29

        Args:
            filename: Name of the CSV file

        Returns:
            Publication timestamp, or None if the filename doesn't carry one
        """
        match = self.FILE_TIMESTAMP_PATTERN.search(filename)
        if not match:
            return None
        try:
            return datetime.datetime.strptime(match.group(1), "%d%m%Y%H%M%S")
        except ValueError:
            return None

    def iter_stores(self, date: datetime.date) -> Generator[Store, None, None]:
        """
        Fetch Plodine's price list ZIP for the date and yield its stores.

        The files are chosen by name first, so that each store can be parsed
        and yielded on its own.

        Args:
            date: The date for which to fetch the price list

        Yields:
            Store objects, each with its products, in the order of each
            store's first file in the ZIP.

        Raises:
            ValueError: If there is no price list ZIP for the date
        """
        zip_url = self.get_index(date)

        with self.open_zip(zip_url) as zip_fp:
            for files in self.select_files(zip_fp, date).values():
                store = self.parse_store_files(zip_fp, files)
                if store:
                    yield store

    def select_files(
        self, zip_fp: ZipFile, date: datetime.date
    ) -> dict[str, list[StoreFile]]:
        """
        Group the CSV files of the date in the ZIP by store, by name only.

        Args:
            zip_fp: The open price list ZIP
            date: Only files published on this date are used

        Returns:
            The files of each store, newest first. Files with the same
            timestamp keep their order in the ZIP. Stores are in the order of
            their first file in the ZIP.
        """
        groups: dict[str, list[StoreFile]] = {}

        for info in zip_fp.infolist():
            filename = info.filename
            if not filename.endswith(".csv"):
                continue

            logger.debug(f"Processing file: {filename}")

            published = self.parse_file_timestamp(filename)
            if published is None:
                logger.warning(f"Skipping CSV {filename}: no timestamp in filename")
                continue
            if published.date() != date:
                logger.debug(
                    f"Skipping CSV {filename}: published on {published:%Y-%m-%d}"
                )
                continue

            store = self.parse_store_from_filename(filename)
            if not store:
                logger.warning(f"Skipping CSV {filename} due to store parsing failure")
                continue

            groups.setdefault(store.store_id, []).append(
                StoreFile(published, info, store)
            )

        for files in groups.values():
            files.sort(key=lambda f: f.published, reverse=True)  # stable

        return groups

    def parse_store_files(
        self, zip_fp: ZipFile, files: list[StoreFile]
    ) -> Store | None:
        """
        Parse the newest of a store's files that can be parsed.

        A file that cannot be read or parsed is logged, and the next newest
        one is tried.

        Args:
            zip_fp: The open price list ZIP
            files: The store's files, newest first

        Returns:
            The store as named in the file used, with its products, or None
            if no file could be parsed.
        """
        for i, file in enumerate(files):
            filename = file.info.filename
            try:
                with zip_fp.open(file.info) as fp:
                    content = fp.read()
            except Exception as e:
                logger.error(f"Error processing file {filename}: {e}", exc_info=True)
                continue

            try:
                products = self.parse_csv(content.decode("utf-8"), delimiter=";")
            except Exception as e:
                logger.error(f"Error processing CSV {filename}: {e}", exc_info=True)
                continue

            for older in files[i + 1 :]:
                logger.debug(
                    f"Skipping CSV {older.info.filename}: newer file for the same store"
                )

            # A copy: `files` keeps its Store, which must not hold the products
            return file.store.model_copy(update={"items": products})

        return None

    def fix_product_data(self, data: dict) -> dict:
        """Mirror the promotional price for the NN 101/2026 format."""
        data = super().fix_product_data(data)

        if data.get("special_sale_type") and data.get("special_price") is None:
            data["special_price"] = data.get("price")

        return data


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    crawler = PlodineCrawler()
    stores = crawler.get_all_products(datetime.date.today())
    print(stores[0])
    print(stores[0].items[0])
