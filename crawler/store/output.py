import re
from collections.abc import Iterable
from csv import DictWriter
from dataclasses import dataclass
from decimal import Decimal
from logging import getLogger
from os import makedirs
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

from common.barcodes import normalize_barcode

from .cities import normalize_city
from .models import Store

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
    # NN 101/2026 availability: 1 = dostupno, 0 = nedostupno, empty = the
    # chain does not publish it. Last, so readers that look columns up by
    # name are unaffected.
    "available",
]


def transform_products(
    stores: list[Store],
) -> tuple[list[dict], list[dict], list[dict]]:
    """
    Transform store data into a structured format for CSV export.

    Args:
        stores: List of Store objects containing product data.

    Returns:
        Tuple containing:
            - List of store dictionaries with STORE_COLUMNS
            - List of product dictionaries with PRODUCT_COLUMNS
            - List of price dictionaries with PRICE_COLUMNS
    """
    store_list = []
    product_map = {}
    price_list = []

    def maybe(val: Decimal | None) -> Decimal | str:
        return val if val is not None else ""

    for store in stores:
        store_data = {
            "store_id": store.store_id,
            "type": store.store_type,
            "address": store.street_address,
            "city": normalize_city(store.city),
            "zipcode": store.zipcode or "",
        }
        store_list.append(store_data)

        for product in store.items:
            key = f"{store.chain}:{product.product_id}"
            if key not in product_map:
                product_map[key] = {
                    "barcode": normalize_barcode(product.barcode) or key,
                    "product_id": product.product_id,
                    "name": product.product,
                    "brand": product.brand,
                    "category": product.category,
                    "unit": product.unit,
                    "quantity": product.quantity,
                }
            price_list.append(
                {
                    "store_id": store.store_id,
                    "product_id": product.product_id,
                    "price": product.price,
                    "unit_price": maybe(product.unit_price),
                    "best_price_30": maybe(product.best_price_30),
                    "anchor_price": maybe(product.anchor_price),
                    "special_price": maybe(product.special_price),
                    "available": (
                        "" if product.available is None else int(product.available)
                    ),
                }
            )

    return store_list, list(product_map.values()), price_list


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
            writer.writerow(
                {
                    k: normalize_whitespace(str(v).strip()) if v is not None else ""
                    for k, v in row.items()
                }
            )


@dataclass
class ChainStats:
    n_stores: int = 0
    n_products: int = 0
    n_prices: int = 0


def save_chain(chain_path: Path, stores: Iterable[Store]) -> ChainStats:
    """
    Save retail chain data to CSV files.

    This function creates a directory for the retail chain and saves:

    * stores.csv - containing store information with STORE_COLUMNS
    * products.csv - containing product information with PRODUCT_COLUMNS
    * prices.csv - containing price information with PRICE_COLUMNS

    Fork change (2026-10-08): prices are written store by store. Since
    NN 101/2026 Plodine publishes ~6M price rows a day, and transforming and
    sorting a whole chain at once peaked at 7 GB and got the crawler killed.
    ``stores`` may be a generator: a crawler that yields one store at a time
    then holds a single store in memory. Each store's items are released once
    written. prices.csv is ordered by product within each store, with stores
    in arrival order; stores.csv and products.csv stay fully sorted.

    Args:
        chain_path: Path to the directory where CSV files will be saved
            (will be created if it doesn't exist).
        stores: Store objects containing product data (list or generator).

    Returns:
        Counts of what was written.
    """

    makedirs(chain_path, exist_ok=True)
    stats = ChainStats()
    store_list: list[dict] = []
    product_map: dict[str, dict] = {}
    prices_fp = None
    prices_writer = None

    try:
        for store in stores:
            s_rows, p_rows, price_rows = transform_products([store])
            store.items = []
            store_list.extend(s_rows)
            for row in p_rows:
                product_map.setdefault(str(row["product_id"]), row)
            if not price_rows:
                continue
            if prices_writer is None:
                prices_fp = open(chain_path / "prices.csv", "w", newline="")
                prices_writer = DictWriter(prices_fp, fieldnames=PRICE_COLUMNS)
                prices_writer.writeheader()
            price_rows.sort(key=lambda x: str(x["product_id"]))
            for row in price_rows:
                prices_writer.writerow(
                    {
                        k: normalize_whitespace(str(v).strip()) if v is not None else ""
                        for k, v in row.items()
                    }
                )
            stats.n_prices += len(price_rows)
    finally:
        if prices_fp is not None:
            prices_fp.close()

    store_list.sort(key=lambda x: str(x["store_id"]))
    product_list = sorted(product_map.values(), key=lambda x: str(x["product_id"]))
    save_csv(chain_path / "stores.csv", store_list, STORE_COLUMNS)
    save_csv(chain_path / "products.csv", product_list, PRODUCT_COLUMNS)

    stats.n_stores = len(store_list)
    stats.n_products = len(product_list)
    return stats


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
