import csv
import re
import shutil
from contextlib import suppress
from csv import DictWriter
from logging import getLogger
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

from common.barcodes import normalize_barcode

from .cities import normalize_city
from .models import Product, Store

logger = getLogger(__name__)

STORE_COLUMNS = [
    "store_id",
    "type",
    "address",
    "city",
    "zipcode",
]

PRODUCT_COLUMNS = [
    "product_id",
    "barcode",
    "name",
    "brand",
    "category",
    "unit",
    "quantity",
]

PRICE_COLUMNS = [
    "store_id",
    "product_id",
    "price",
    "unit_price",
    "best_price_30",
    "anchor_price",
    "special_price",
    "available",
    "special_sale_type",
    "anchor_price_date",
]


def store_row(store: Store) -> dict:
    """The row of a store in stores.csv, before cleaning."""
    return {
        "store_id": store.store_id,
        "type": store.store_type,
        "address": store.street_address,
        "city": normalize_city(store.city),
        "zipcode": store.zipcode,
    }


def product_row(chain: str, product: Product) -> dict:
    """The row of a product in products.csv, before cleaning."""
    return {
        "barcode": normalize_barcode(product.barcode)
        or f"{chain}:{product.product_id}",
        "product_id": product.product_id,
        "name": product.product,
        "brand": product.brand,
        "category": product.category,
        "unit": product.unit,
        "quantity": product.quantity,
    }


def price_row(store: Store, product: Product) -> dict:
    """The row of a product's price in a store in prices.csv, before cleaning."""
    return {
        "store_id": store.store_id,
        "product_id": product.product_id,
        "price": product.price,
        "unit_price": product.unit_price,
        "best_price_30": product.best_price_30,
        "anchor_price": product.anchor_price,
        "special_price": product.special_price,
        "available": None if product.available is None else int(product.available),
        "special_sale_type": product.special_sale_type,
        "anchor_price_date": product.anchor_price_date,
    }


def normalize_whitespace(value: str) -> str:
    """
    Normalize whitespace in a string by replacing multiple whitespace
    characters (spaces, tabs, newlines, etc.) with a single space.

    Args:
        value: String to normalize

    Returns:
        String with normalized whitespace
    """
    return re.sub(r"\s+", " ", value)


def clean_row(row: dict) -> dict[str, str]:
    """A row as written to the CSV: strings, whitespace normalized."""
    return {
        k: normalize_whitespace(str(v).strip()) if v is not None else ""
        for k, v in row.items()
    }


def save_csv(path: Path, data: list[dict], columns: list[str]):
    """
    Save data to a CSV file.

    Args:
        path: Path to the CSV file.
        data: List of dictionaries containing the data to save.
        columns: List of column names for the CSV file.
    """
    if not data:
        logger.warning(f"No data to save at {path}, skipping")
        return

    if set(columns) != set(data[0].keys()):
        raise ValueError(
            f"Column mismatch: expected {columns}, got {list(data[0].keys())}"
        )
        return

    with open(path, "w", newline="") as f:
        writer = DictWriter(f, fieldnames=columns)
        writer.writeheader()
        for row in data:
            writer.writerow(clean_row(row))


class ChainWriter:
    """
    Save one retail chain's data, taking its stores one at a time.

    Writes to the chain directory:

    * stores.csv - containing store information with STORE_COLUMNS
    * products.csv - containing product information with PRODUCT_COLUMNS
    * prices.csv - containing price information with PRICE_COLUMNS

    `add` writes each store's price rows to a part file in `<path>/.parts`, so
    the caller can drop the store's products. `finish` writes the CSV files
    and removes the part files. Only the store rows and one row per distinct
    product stay in memory, and in `finish` the price rows of one store.

    The CSV files are sorted by store ID, then product ID. Rows with equal
    keys keep the order in which they were added: two stores with the same
    store ID, or a product listed twice in a store. For each product ID,
    products.csv has the product as the first store added lists it.
    """

    PARTS_DIR = ".parts"

    def __init__(self, path: Path):
        """
        Args:
            path: Directory for the CSV files. Created by the first `add`.
        """
        self.path = path
        self.parts_path = path / self.PARTS_DIR
        self.n_stores = 0
        self.n_prices = 0

        self._created_path = False
        self._stores: list[dict] = []
        # product_id -> products.csv row of the first product seen
        self._products: dict[str, dict] = {}
        # store_id -> part files of the stores with that ID, in the order added
        self._parts: dict[str, list[Path]] = {}

        # Part files of a run that was killed
        shutil.rmtree(self.parts_path, ignore_errors=True)

    @property
    def n_products(self) -> int:
        """Number of distinct products in the stores added."""
        return len(self._products)

    def add(self, store: Store) -> None:
        """
        Add a complete store with its products.

        If this raises, call `abort`: the writer is not usable after it.
        """
        if not self.n_stores:
            self._created_path = not self.path.exists()
            self.parts_path.mkdir(parents=True, exist_ok=True)

        self._stores.append(store_row(store))

        # Numbered, not named by store ID: store IDs repeat, and IDs that differ
        # only in case are the same file name on macOS and Windows.
        part = self.parts_path / f"{self.n_stores + 1:06d}.csv"
        with open(part, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            for product in store.items:
                if product.product_id not in self._products:
                    self._products[product.product_id] = product_row(
                        store.chain, product
                    )

                # The raw product ID first, as the sort key in `finish`
                row = clean_row(price_row(store, product))
                writer.writerow(
                    [product.product_id] + [row[col] for col in PRICE_COLUMNS]
                )

        self._parts.setdefault(store.store_id, []).append(part)
        self.n_stores += 1
        self.n_prices += len(store.items)

    def finish(self) -> None:
        """
        Write the CSV files and remove the part files.

        A file without rows is not written, as with `save_csv`.
        """
        try:
            self._stores.sort(key=lambda row: row["store_id"])
            save_csv(self.path / "stores.csv", self._stores, STORE_COLUMNS)

            products = sorted(
                self._products.values(), key=lambda row: row["product_id"]
            )
            save_csv(self.path / "products.csv", products, PRODUCT_COLUMNS)

            self._write_prices(self.path / "prices.csv")
        finally:
            shutil.rmtree(self.parts_path, ignore_errors=True)

    def _write_prices(self, path: Path) -> None:
        if not self.n_prices:
            logger.warning(f"No data to save at {path}, skipping")
            return

        with open(path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(PRICE_COLUMNS)
            for store_id in sorted(self._parts):
                rows: list[list[str]] = []
                for part in self._parts[store_id]:
                    with open(part, newline="", encoding="utf-8") as pf:
                        rows.extend(csv.reader(pf))
                rows.sort(key=lambda row: row[0])  # stable
                writer.writerows(row[1:] for row in rows)

    def abort(self) -> None:
        """Remove the part files, and the chain directory if `add` created it."""
        shutil.rmtree(self.parts_path, ignore_errors=True)
        if self._created_path:
            # Empty unless something else wrote to it; then keep it
            with suppress(OSError):
                self.path.rmdir()


def copy_archive_info(path: Path):
    archive_info = open(Path(__file__).parent / "archive-info.txt", "r").read()
    with open(path / "archive-info.txt", "w") as f:
        f.write(archive_info)


def create_archive(path: Path, output: Path):
    """
    Create a ZIP archive of price files for a given date.

    Args:
        path: Path to the directory to archive.
        output: Path to the output ZIP file.
    """
    with ZipFile(output, "w", compression=ZIP_DEFLATED, compresslevel=9) as zf:
        for file in path.rglob("*"):
            zf.write(file, arcname=file.relative_to(path))
