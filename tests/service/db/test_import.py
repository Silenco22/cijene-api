import asyncio
import importlib
from datetime import date, datetime
from decimal import Decimal

import pytest

# "import" is a keyword, so the module can't be imported with a statement
importer = importlib.import_module("service.db.import")

OLD_HEADER = (
    "store_id,product_id,price,unit_price,best_price_30,anchor_price,special_price"
)
NEW_HEADER = OLD_HEADER + ",available,special_sale_type,anchor_price_date"


class _FakeDatabase:
    def __init__(self):
        self.prices = []
        self.eans = []
        self.chain_products = []

    async def add_many_prices(self, prices):
        self.prices.extend(prices)
        return len(prices)

    async def get_product_barcodes(self):
        return {}

    async def get_chain_product_map(self, chain_id):
        return {p.code: i for i, p in enumerate(self.chain_products, 1)}

    async def add_ean(self, ean):
        self.eans.append(ean)
        return len(self.eans)

    async def add_many_chain_products(self, products):
        self.chain_products.extend(products)
        return len(products)


@pytest.fixture
def db(monkeypatch):
    fake = _FakeDatabase()
    monkeypatch.setattr(importer, "db", fake)
    return fake


def _import(tmp_path, *lines):
    path = tmp_path / "prices.csv"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return asyncio.run(
        importer.process_prices(
            date(2026, 11, 17),
            path,
            chain_id=1,
            store_map={"S1": 10},
            chain_product_map={"P1": 20, "P2": 21, "P3": 22},
        )
    )


def test_new_columns_are_imported(db, tmp_path):
    _import(
        tmp_path,
        NEW_HEADER,
        "S1,P1,1.29,1.29,,1.19,0.99,1,Akcija,2025-05-02",
        "S1,P2,2.49,2.49,,,,0,,",
        "S1,P3,3.99,3.99,,,,,,",
    )

    p1, p2, p3 = db.prices
    assert (p1.available, p1.special_sale_type, p1.anchor_price_date) == (
        True,
        "Akcija",
        date(2025, 5, 2),
    )
    assert p1.anchor_price == Decimal("1.19")
    assert (p2.available, p2.special_sale_type, p2.anchor_price_date) == (
        False,
        None,
        None,
    )
    assert (p3.available, p3.special_sale_type, p3.anchor_price_date) == (
        None,
        None,
        None,
    )


def test_archives_without_new_columns_still_import(db, tmp_path):
    _import(tmp_path, OLD_HEADER, "S1,P1,1.29,1.29,,1.19,0.99")

    [price] = db.prices
    assert price.special_price == Decimal("0.99")
    assert (price.available, price.special_sale_type, price.anchor_price_date) == (
        None,
        None,
        None,
    )


def test_product_with_a_too_long_barcode_is_skipped(db, tmp_path):
    # ZooCity, 2026-11-26: the product name in the code column, no barcode
    name = "Hunter oprsnica za pse Aalborg Mixed tamnosmeđa/siva"
    path = tmp_path / "products.csv"
    path.write_text(
        "product_id,barcode,name,brand,category,unit,quantity\n"
        "10045,3182550728102,Royal Canin Chihuahua,,,KG,\n"
        f"{name},zoocity:{name},{name},Hunter,,,\n",
        encoding="utf-8",
    )

    product_map = asyncio.run(importer.process_products(path, 1, "zoocity", {}))

    assert list(product_map) == ["10045"]
    assert db.eans == ["3182550728102"]


def test_a_failing_chain_does_not_stop_the_import(db, tmp_path, monkeypatch):
    for code in ("konzum", "zoocity", "spar"):
        (tmp_path / code).mkdir()
    imported = []
    stats = []

    async def process_chain(price_date, chain_dir, barcodes):
        if chain_dir.name == "zoocity":
            raise ValueError("value too long")
        imported.append(chain_dir.name)

    async def compute_stats(price_date):
        stats.append(price_date)

    monkeypatch.setattr(importer, "process_chain", process_chain)
    monkeypatch.setattr(importer, "compute_stats", compute_stats)

    failed = asyncio.run(importer._import(tmp_path, datetime(2026, 11, 26)))

    assert failed == ["zoocity"]
    assert sorted(imported) == ["konzum", "spar"]
    assert stats == [datetime(2026, 11, 26)]
