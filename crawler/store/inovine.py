import datetime
import logging
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from crawler.store.models import Store

from .base import BaseCrawler, CrawlerBlocked

logger = logging.getLogger(__name__)


class INovineCrawler(BaseCrawler):
    """
    Crawler for iNovine (newsstands).

    The price-list page has a store dropdown and links each store's latest
    files: a goods list (ROBA) and a services list (USLUGA), exported at
    about 03:15 local time on the day they are for. Each store's archive page
    links its earlier files. The file URLs have random names, so the crawler
    reads them from the page. Store type, address, postcode and city come
    from the dropdown text.

    The CSVs are in the rulebook 1584 format: UTF-8 with a BOM,
    semicolon-separated, one price column and a DA/NE sale flag.
    """

    CHAIN = "inovine"
    INDEX_URL = "https://www.inovine.hr/cijene/"
    ARCHIVE_URL = INDEX_URL + "?invictus_trgovina={value}"

    # Kiosk 009 - Vukovarska 120 (autobusna stanica) 51000 Rijeka
    # Kiosk 770 - 770 - Savski gaj bb, Zagreb 10000 Zagreb
    STORE_PATTERN = re.compile(r"^(.+?) (\d+) - (.+) (\d{5}) (.+)$")

    STORE_TYPES = {
        "Kiosk": "kiosk",
        "Nespecijalizirana prodavaonica pretežno neprehrambenih proizvoda": "prodavaonica",
    }

    # 85031837779#KIOSK#Vukovarska_120_(autobusna_stanica)_51000_Rijeka#009#ROBA#26330#20261126_0315.csv
    # "ROBA" is the goods list; the services list ("USLUGA") does not match.
    FILE_PATTERN = re.compile(r"#(\d+)#ROBA#\d+#(\d{8})_(\d{4})\.csv$")

    PRICE_MAP = {
        "price": ("MP cijena", False),
        "unit_price": ("Cijena za jedinicu mj.", False),
        "anchor_price": ("Bazna cijena", False),
    }

    # "EAN barkod" has the same values as "Barkod"
    FIELD_MAP = {
        "product": ("Naziv artikla", True),
        "product_id": ("Šifra artikla", True),
        "brand": ("Marka", False),
        "quantity": ("Neto količina", False),
        "unit": ("Jedinica mjere", False),
        "barcode": ("Barkod", False),
        "category": ("", False),
    }

    # DA/NE. There is no separate special price and no name for the sale.
    BOOL_MAP = {
        "on_special": ("Akcija", False),
    }

    # Without the flag, sale prices would pass as regular prices.
    REQUIRED_COLUMNS = [
        "Naziv artikla",
        "Šifra artikla",
        "Marka",
        "Jedinica mjere",
        "MP cijena",
        "Akcija",
        "Barkod",
        "Bazna cijena",
    ]

    # Rulebook 1584: net quantity and unit price only where they apply
    OPTIONAL_COLUMNS = [
        "Neto količina",
        "Cijena za jedinicu mj.",
    ]

    # The "Bazna cijena" header states no date
    ANCHOR_PRICE_DATE = None

    def get_stores(self, soup: BeautifulSoup) -> dict[str, tuple[str, str]]:
        """
        Read the stores from the dropdown on the price-list page.

        Args:
            soup: The price-list page

        Returns:
            Dropdown value and store text by store code
        """
        stores = {}
        for option in soup.select('select[name="invictus_trgovina"] option[value]'):
            value = str(option["value"]).strip()
            if not value:
                continue
            # o-009 -> 009
            code = value.removeprefix("o-")
            stores[code] = (value, " ".join(option.get_text().split()))
        return stores

    def get_files(self, soup: BeautifulSoup) -> set[tuple[str, str, str, str]]:
        """
        Read the goods price lists linked on a page.

        An archive page also has the main list, so a file can be linked
        twice; the set keeps it once.

        Args:
            soup: The price-list page or a store's archive page

        Returns:
            Store code, date (YYYYMMDD), export time (HHMM) and URL per file
        """
        files = set()
        for link in soup.select("a.invictus-cjenik-datoteke-ime-link[href]"):
            # The link text is the original file name; the URL has a random name
            m = self.FILE_PATTERN.search(link.get_text(strip=True))
            if m:
                code, day, time = m.groups()
                url = urljoin(self.INDEX_URL, str(link["href"]))
                files.add((code, day, time, url))
        return files

    def select_files(
        self, files: set[tuple[str, str, str, str]], date: datetime.date
    ) -> dict[str, str]:
        """
        Pick each store's file for the date.

        Args:
            files: Files from get_files
            date: The date of the price lists

        Returns:
            File URL by store code
        """
        day = f"{date:%Y%m%d}"
        urls = {}
        # Sorted by code and time, so a later export replaces an earlier one
        for code, file_day, _, url in sorted(files):
            if file_day != day:
                continue
            if code in urls:
                logger.info(f"iNovine {code} has more than one file for {date}")
            urls[code] = url
        return urls

    def make_store(self, code: str, text: str) -> Store:
        """
        Build a store from its dropdown text.

        Args:
            code: Store code
            text: Dropdown text: type, code, address, postcode and city

        Returns:
            The store, without products
        """
        m = self.STORE_PATTERN.match(text)
        if not m or m.group(2) != code:
            logger.warning(
                f"Unexpected iNovine store name {text!r}, keeping it without details"
            )
            return Store(
                chain=self.CHAIN,
                store_id=code,
                name=text,
                store_type="",
                city="",
                street_address="",
            )

        type_text, _, address, zipcode, city = m.groups()
        return Store(
            chain=self.CHAIN,
            store_id=code,
            name=f"{type_text} {code}",
            store_type=self.STORE_TYPES.get(type_text, type_text.lower()),
            street_address=address,
            zipcode=zipcode,
            city=city,
        )

    def get_all_products(self, date: datetime.date) -> list[Store]:
        index = BeautifulSoup(self.fetch_text(self.INDEX_URL), "html.parser")
        store_list = self.get_stores(index)
        if not store_list:
            raise ValueError("No stores in the iNovine store dropdown")

        index_files = self.get_files(index)
        if not index_files:
            raise ValueError("No goods price lists on the iNovine page")

        # Before the day's export, or a future date: the archive pages would
        # have nothing either
        if f"{date:%Y%m%d}" > max(day for _, day, _, _ in index_files):
            logger.info(f"No iNovine price list for {date}")
            return []

        urls = self.select_files(index_files, date)

        stores = []
        missing = []
        for code, (value, text) in sorted(store_list.items()):
            try:
                url = urls.get(code)
                if url is None:
                    archive_url = self.ARCHIVE_URL.format(value=value)
                    archive = BeautifulSoup(self.fetch_text(archive_url), "html.parser")
                    url = self.select_files(self.get_files(archive), date).get(code)
                if url is None:
                    missing.append(code)
                    continue

                content = self.fetch_text(url, encodings=["utf-8-sig"])
                products = self.parse_csv(content, delimiter=";")
            except CrawlerBlocked:
                raise
            except Exception as e:
                logger.error(f"Error processing iNovine {code}: {e}", exc_info=True)
                continue

            if not products:
                logger.warning(f"iNovine {code} has no products for {date}, skipping")
                continue

            store = self.make_store(code, text)
            store.items = products
            stores.append(store)

        if not stores:
            logger.info(f"No iNovine price list for {date}")
            return []

        if missing:
            logger.info(f"iNovine: no price list for {date} in {', '.join(missing)}")
        return stores

    def fix_product_data(self, data: dict) -> dict:
        on_special = data.pop("on_special", None)
        # Imported magazines: ".075110084301" is the UPC 075110084301
        data["barcode"] = data["barcode"].removeprefix(".")
        data = super().fix_product_data(data)

        # One price column plus a DA/NE flag: on sale rows the published price
        # is the sale price, and the regular price is not published.
        if on_special:
            data["special_price"] = data["price"]

        return data


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    crawler = INovineCrawler()
    stores = crawler.get_all_products(datetime.date.today())
    print(stores[0])
    print(stores[0].items[0])
