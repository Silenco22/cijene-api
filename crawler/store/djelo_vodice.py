import csv
import datetime
import logging
import re
from collections.abc import Iterable
from io import BytesIO
from tempfile import TemporaryFile
from urllib.parse import unquote

import openpyxl
from bs4 import BeautifulSoup

from crawler.store.models import Product, Store

from .base import BaseCrawler, CrawlerBlocked

logger = logging.getLogger(__name__)


def cell_str(val):
    """Convert a cell value to string, treating None as empty but preserving 0/0.0."""
    return "" if val is None else str(val).strip()


# Decoded file name since 2026-11-24: "#"-separated OIB, store type, place,
# store id, kind of price list (ROBA for goods, USLUGA for services), file id
# and timestamp. The place is street address, zip code and city, and spaces in
# it are "_".
#   11279847062#PRODAVAONICA#ČIKOLE_2_22320_DRNIŠ#02044#ROBA#26330#20261126_0307.csv
FILENAME_PATTERN = re.compile(
    r"^\d+#(?P<store_type>[^#]*)#(?P<place>[^#]*)#(?P<store_id>\d+)#"
    r"(?P<kind>[^#]*)#\d+#(?P<date>\d{8})_(?P<time>\d{4})\.csv$"
)
ADDRESS_PLACE_PATTERN = re.compile(
    r"^(?P<address>.*?)_(?P<zipcode>\d{5})_(?P<city>.+)$"
)

# Decoded file name until 2026-11-23: "#"-separated store type, street address,
# place, store id, file id and timestamp. The place is "ZIP CITY" since
# 2026-11-14 and only the city before that. Older names lost their diacritics
# to "?". The files are CSV since 2026-11-16 10:19, XLSX before that.
#   PRODAVAONICA#ČIKOLE 2#22320 DRNIŠ#02044#26321#2026-11-17T010554.csv
#   PRODAVAONICA#ČIKOLE 2#22320 DRNIŠ#02044#26318#2026-11-14T010557.xlsx
#   SUPERMARKET#PUT GA?ELEZA 5A#VODICE#02010#26317#2026-11-13T010506.xlsx
OLD_FILENAME_PATTERN = re.compile(
    r"^(?P<store_type>[^#]*)#(?P<address>[^#]*)#(?P<place>[^#]*)#"
    r"(?P<store_id>\d+)#\d+#(?P<date>\d{4}-\d{2}-\d{2})T(?P<time>\d{6})"
    r"\.(?:xlsx|csv)$"
)
PLACE_PATTERN = re.compile(r"^(?P<zipcode>\d{5})\s+(?P<city>.+)$")


def parse_filename(name: str) -> tuple[str, str, dict[str, str]] | None:
    """
    Read a decoded file name in either format.

    Returns:
        The publication date (YYYY-MM-DD), the time (HHMMSS), and the store
        info: store id, store type, street address, city and zip code. None if
        the name is in neither format, or is a price list of services.
    """
    if m := FILENAME_PATTERN.match(name):
        if m["kind"] != "ROBA":
            return None
        pm = ADDRESS_PLACE_PATTERN.match(m["place"])
        address, zipcode, city = pm.groups() if pm else (m["place"], "", "")
        d = m["date"]
        date = f"{d[:4]}-{d[4:6]}-{d[6:]}"
        time = f"{m['time']}00"
    elif m := OLD_FILENAME_PATTERN.match(name):
        address = m["address"]
        place = m["place"].strip()
        pm = PLACE_PATTERN.match(place)
        zipcode, city = (pm["zipcode"], pm["city"]) if pm else ("", place)
        date, time = m["date"], m["time"]
    else:
        return None

    info = {
        "store_id": m["store_id"],
        "store_type": m["store_type"].replace("_", " ").strip().lower(),
        "street_address": address.replace("_", " ").strip(),
        "city": city.replace("_", " ").strip(),
        "zipcode": zipcode,
    }
    return date, time, info


# Store metadata keyed by store_id (from XLSX column A). Overrides what the file
# name says, for stores whose name lacks the address or spells it wrong.
STORES = {
    "02010": {
        "name": "Dućan 10 Vodice",
        "store_type": "supermarket",
        "city": "Vodice",
        "street_address": "Put Gaćeleza 5A",
        "zipcode": "22211",
    },
    # File name: "PUT BILOSTANA 1", no place.
    "02066": {
        "city": "Krković",
        "street_address": "Put Bilostanova 1",
        "zipcode": "22214",
    },
}

# Points of sale that publish a price list but are not shops. Their file names
# carry no address.
SKIPPED_STORES = {
    "05075": "Restoran Vodice",
    "08001": "Frizerski salon Vodice",
}


class DjeloVodiceCrawler(BaseCrawler):
    """
    Crawler for Djelo Vodice store prices.

    Djelo Vodice publishes one pricelist per store per day as a directory
    listing, and the store's address is in the file name. Until 2026-11-13 the
    listing had only the Vodice supermarket (02010). From 2026-11-14 it has
    about 38 points of sale, mostly shops around Šibenik. The files were XLSX
    until 2026-11-16 and are CSV (";", UTF-8) since, with the same columns.
    On 2026-11-24 the file names and the columns changed (see the column
    lists). Each file is read by the column names in the header row.
    """

    CHAIN = "djelo_vodice"
    BASE_URL = "https://dv10.djelo-vodice.hr"

    # Not used — parse_excel reads the XLSX itself, so the shared map-driven
    # parser never runs here.
    PRICE_MAP = {}
    FIELD_MAP = {}

    # On 2026-11-24 "Sidrena cijena" became "Bazna cijena" (NN 132/2026), and
    # "Obr. jedinica mjere" (KILOGRAM, the unit of the unit price) was dropped,
    # which leaves "Jedinica mjere" (KOMAD etc.). The store columns were
    # dropped too; the store id comes from the file name.
    REQUIRED_COLUMNS = [
        "Šifra artikla",
        "Naziv artikla",
        ["Sidrena cijena", "Bazna cijena"],
        "Barkod",
        "MP cijena",
        "Cijena za jedinicu mj.",
        ["Obr. jedinica mjere", "Jedinica mjere"],
    ]

    # "Akcija" (Da/Ne promo flag) appeared on 2026-11-14, "Marka" and "Neto
    # količina" on 2026-11-24. "Kategorija" (dropped 2026-11-14) and
    # "Raspoloživo" (2026-11-14 to 2026-11-23) are still read from older files,
    # but not listed here, because they would warn on every file.
    # On "Akcija" rows "MP cijena" is the promo price, so it is also the
    # special price. The flag doesn't name the form of sale, so
    # special_sale_type stays empty.
    OPTIONAL_COLUMNS = [
        "Akcija",
        "Marka",
        "Neto količina",
    ]

    def get_index(self, date: datetime.date) -> list[tuple[str, dict[str, str]]]:
        """
        Find the price list file of each store for the given date.

        If a store has more than one file for the date, the latest one wins.

        Args:
            date: Target date to find the pricelists for.

        Returns:
            (URL, store info) for each store, where the store info holds the
            store id, store type, street address, city and zip code from the
            file name.

        Raises:
            ValueError: If no file is found for the target date.
        """
        content = self.fetch_text(f"{self.BASE_URL}/")
        soup = BeautifulSoup(content, "html.parser")

        target = date.isoformat()
        latest: dict[str, tuple[str, str, dict[str, str]]] = {}

        for link in soup.select('a[href$=".xlsx"], a[href$=".csv"]'):
            href = link.get("href")
            if not href:
                continue

            parsed = parse_filename(unquote(str(href)))
            if not parsed or parsed[0] != target:
                continue

            _, time, info = parsed
            store_id = info["store_id"]
            if store_id in SKIPPED_STORES:
                continue

            if store_id not in latest or time > latest[store_id][0]:
                latest[store_id] = (time, f"{self.BASE_URL}/{href}", info)

        if not latest:
            raise ValueError(f"No price list found for date {date}")

        return [(url, info) for _, url, info in latest.values()]

    def parse_excel(
        self, excel_data: bytes, store_info: dict[str, str]
    ) -> tuple[Store, list[Product]]:
        """
        Parse an XLSX file into a Store object and list of Product objects.

        The XLSX has a single sheet "Cijene na dan" with a header row.

        Args:
            excel_data: Raw XLSX file content as bytes.
            store_info: Store info from the file name, see parse_rows.

        Returns:
            Tuple of (Store, list of Products).

        Raises:
            ValueError: If a required column is missing.
        """
        workbook = openpyxl.load_workbook(BytesIO(excel_data), data_only=True)
        worksheet = workbook.active

        if not worksheet:
            raise ValueError("No active worksheet found in the Excel file")

        return self.parse_rows(worksheet.iter_rows(values_only=True), store_info)

    def parse_csv_file(
        self, text: str, store_info: dict[str, str]
    ) -> tuple[Store, list[Product]]:
        """
        Parse a CSV price list into a Store object and list of Product objects.

        Until 2026-11-23 the CSV has the same columns as the XLSX. It is
        separated by ";", with decimal commas.

        Args:
            text: CSV file content.
            store_info: Store info from the file name, see parse_rows.

        Returns:
            Tuple of (Store, list of Products).

        Raises:
            ValueError: If a required column is missing.
        """
        rows = csv.reader(text.splitlines(), delimiter=";")
        return self.parse_rows((tuple(row) for row in rows), store_info)

    def parse_rows(
        self, rows: Iterable[tuple], store_info: dict[str, str]
    ) -> tuple[Store, list[Product]]:
        """
        Parse the rows of a price list, header first, into a Store and Products.

        Args:
            rows: Rows of cell values (XLSX) or strings (CSV).
            store_info: Store id, type, address, city and zip code from the
                file name. Entries in STORES take precedence.

        Returns:
            Tuple of (Store, list of Products).

        Raises:
            ValueError: If a required column is missing.
        """
        rows = iter(rows)
        header = [cell_str(name) for name in next(rows, ())]
        self.check_columns(header)
        index = {name.lower(): i for i, name in enumerate(header) if name}

        def cell(row: tuple, *columns: str) -> str:
            """Value of the first of the columns the file has."""
            for column in columns:
                i = index.get(column.lower())
                if i is not None:
                    return cell_str(row[i]) if i < len(row) else ""
            return ""

        store_id = store_info["store_id"]
        info = {**store_info, **STORES.get(store_id, {})}
        city = info.get("city", "")
        store = Store(
            chain=self.CHAIN,
            store_id=store_id,
            name=info.get("name", f"Djelo Vodice {city}"),
            store_type=info.get("store_type", ""),
            city=city,
            street_address=info.get("street_address", ""),
            zipcode=info.get("zipcode", ""),
        )
        if not city:
            logger.warning(
                f"No city for store_id '{store_id}', "
                f"add it to the STORES map in djelo_vodice.py"
            )

        products = []
        for row_idx, row in enumerate(rows, start=2):
            if not any(cell_str(value) for value in row):
                continue

            try:
                data = {
                    "product_id": cell(row, "Šifra artikla"),
                    "product": cell(row, "Naziv artikla"),
                    "anchor_price": self.parse_price(
                        cell(row, "Sidrena cijena", "Bazna cijena"), False
                    ),
                    "barcode": cell(row, "Barkod"),
                    "category": cell(row, "Kategorija"),
                    "price": self.parse_price(cell(row, "MP cijena"), True),
                    "unit_price": self.parse_price(
                        cell(row, "Cijena za jedinicu mj."), True
                    ),
                    "unit": cell(row, "Obr. jedinica mjere", "Jedinica mjere"),
                    "available": self.parse_bool(cell(row, "Raspoloživo")),
                    "brand": cell(row, "Marka"),
                    "quantity": cell(row, "Neto količina"),
                }
                if self.parse_bool(cell(row, "Akcija")):
                    data["special_price"] = data["price"]
                data = self.fix_product_data(data)
                products.append(Product(**data))
            except Exception as e:
                row_txt = "; ".join("" if v is None else str(v) for v in row)
                logger.warning(f"Failed to parse row {row_idx}: `{row_txt}`: {e}")
                continue

        logger.debug(f"Parsed {len(products)} products for store {store_id}")
        return store, products

    def get_all_products(self, date: datetime.date) -> list[Store]:
        """
        Fetch and parse all products for the given date.

        Args:
            date: The date to fetch the pricelists for.

        Returns:
            List of stores with their products.
        """
        files = self.get_index(date)
        logger.info(f"Found {len(files)} price lists for {date}")
        stores = []

        for url, store_info in files:
            try:
                if url.endswith(".csv"):
                    text = self.fetch_text(url, encodings=["utf-8-sig", "windows-1250"])
                    store, products = self.parse_csv_file(text, store_info)
                else:
                    with TemporaryFile(mode="w+b") as temp_file:
                        self.fetch_binary(url, temp_file)
                        temp_file.seek(0)
                        excel_data = temp_file.read()

                    store, products = self.parse_excel(excel_data, store_info)
            except CrawlerBlocked:
                raise
            except Exception as e:
                logger.error(f"Error processing {url}: {e}", exc_info=True)
                continue

            if not products:
                logger.warning(f"No products found in {url}")
                continue

            store.items = products
            stores.append(store)

        return stores


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    crawler = DjeloVodiceCrawler()
    stores = crawler.crawl(datetime.date.today())
    print(stores[0])
    print(stores[0].items[0])
