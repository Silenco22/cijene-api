# Crawler Architecture Documentation

## Overview

The crawler system extracts price data from the websites of Croatian retail chains. Each chain has its own crawler class. All crawlers inherit from `BaseCrawler` and return the same data model (`Store` and `Product`), so the output code is shared by all chains.

## Architecture Overview

### Core Components

```
crawler/
├── crawl.py                 # CRAWLERS registry, crawl_chain(), crawl()
├── cli/
│   ├── crawl.py             # Command-line crawler: one date, one or more chains
│   └── daily.py             # Daily runner: one process per chain, re-crawls incomplete chains
└── store/
    ├── __init__.py
    ├── base.py              # BaseCrawler and CrawlerBlocked
    ├── models.py            # Product and Store data models
    ├── output.py            # CSV output and ZIP archive creation
    ├── cities.py            # Canonical city names for stores.csv
    ├── utils.py             # Common utility functions
    ├── archive-info.txt     # Archive description (in Croatian), copied into each ZIP
    └── [chain_name].py      # Individual store implementations
```

The output code also uses `common/barcodes.py` (barcode normalization) and `enrichment/cities.csv` (city name map).

### Data Flow

1. **Initialization**: `crawl_chain()` creates the crawler. `BaseCrawler.__init__()` creates the HTTP client and prepares the column maps.
2. **Index Fetching**: The crawler downloads the index page or API response and selects the files for the requested date.
3. **Data Retrieval**: The crawler downloads the price lists (CSV, XML, XLSX or JSON). All requests go through the pacing and retry logic in `BaseCrawler`.
4. **Data Parsing**: The crawler checks the columns of each file and parses the rows into `Product` objects, grouped by `Store`.
5. **Output Generation**: `save_chain()` writes the CSV files of the chain. `crawl()` then adds `archive-info.txt` and creates the ZIP archive.

## Base Crawler Class

### BaseCrawler (`base.py`)

All store crawlers inherit from `BaseCrawler`, which provides:

#### Required Class Attributes

```python
class MyCrawler(BaseCrawler):
    CHAIN = "store_name"           # Lowercase chain identifier
    BASE_URL = "https://..."       # Base website URL
    INDEX_URL = "https://..."      # Price list index URL (a convention; BaseCrawler does not use it)

    # Price field mappings: field_name -> (column_name, is_required)
    PRICE_MAP = {
        "price": ("MPC", True),
        # A tuple (or list) gives accepted spellings; the first one present in the file is used
        "unit_price": (("Cijena za jedinicu mjere", "Cijena po JM"), True),
        "special_price": ("Akcijska cijena", False),
        "best_price_30": ("Najniža cijena 30 dana", False),
        "anchor_price": ("Sidrena cijena na 2.5.2025", False),
    }

    # Other field mappings
    FIELD_MAP = {
        "product": ("Naziv proizvoda", True),
        "product_id": ("Šifra proizvoda", True),
        "brand": ("Marka", False),
        "quantity": ("Količina", False),
        "unit": ("Jedinica mjere", False),
        "barcode": ("Barkod", False),
        "category": ("Kategorija", False),
        "special_sale_type": ("Naziv posebnog oblika prodaje", False),
    }

    # Columns each source file must contain (file level, see below)
    REQUIRED_COLUMNS = [
        "MPC",
        ("Cijena za jedinicu mjere", "Cijena po JM"),  # either name is accepted
        "Sidrena cijena na 2.5.2025",
        "Naziv proizvoda",
        "Šifra proizvoda",
        "Marka",
        "Jedinica mjere",
        "Barkod",
    ]
```

`BaseCrawler.__init__()` reads `PRICE_MAP`, `FIELD_MAP` and `REQUIRED_COLUMNS`, so every crawler must declare them. dm and Djelo Vodice do not use the maps and set them to `{}`.

The maps must produce the keys that `fix_product_data()` and `Product` need: `price` and `unit_price` in `PRICE_MAP`, and `product`, `product_id`, `brand`, `quantity`, `unit`, `barcode` and `category` in `FIELD_MAP`, unless an override fills them in (for example, `LorencoCrawler.fix_product_data()` sets `product_id`, `brand`, `category` and `quantity`).

#### Optional Configuration

```python
class MyCrawler(BaseCrawler):
    BOOL_MAP = {"available": ("Dostupnost", False)}  # Boolean fields (default: {})
    OPTIONAL_COLUMNS = ["Kategorija"]                # Columns the parser can do without (default: [])
    ANCHOR_PRICE_DATE = datetime.date(2025, 5, 2)    # Date stated in the anchor price header (default: None)

    TIMEOUT = 30.0                 # HTTP timeout in seconds
    USER_AGENT = None              # Declared, but BaseCrawler does not use it
    VERIFY_TLS_CERT = True         # TLS certificate verification
    ZIP_DATE_PATTERN = re.compile(r"...") # Regex for date extraction from ZIP URLs (default: None)

    # Retries and pacing, see "Retries and Request Pacing" below
    MAX_RETRIES = 6                # Total attempts per request, the first attempt included
    REQUEST_DELAY = 0.0            # Minimum seconds between two requests to the same host
    RETRY_BACKOFF_BASE = 1.0       # Wait before the 2nd attempt; doubles per attempt
    RETRY_BACKOFF_MAX = 30.0       # Ceiling on the backoff
    RETRY_AFTER_MAX = 120.0        # Longest Retry-After that is waited out
    RETRY_STATUS = frozenset({408, 425, 429})   # Non-5xx statuses that are retried
    BLOCKED_STATUS = frozenset({403})           # Statuses that raise CrawlerBlocked
```

#### Core Methods

- `fetch_text(url, encodings=None, prefix=None, attempts=None)`: Download text content. With `encodings`, try each encoding in order. With `prefix`, use the first encoding whose decoded text starts with `prefix`.
- `fetch_binary(url, fp, attempts=None)`: Stream binary content into an open file (for example a `NamedTemporaryFile`). The file is truncated before each attempt.
- `post_form(url, data, headers=None, attempts=None)`: POST a form, with the same pacing and retries as a download (used by Boso).
- `read_csv(text, delimiter=",")`: Return a `DictReader` for the text.
- `parse_csv(content, delimiter=",")`: Check the header with `check_columns()`, then parse each row with `parse_csv_row()`. Rows that fail are logged and skipped. Raises `ValueError` if the header row is missing or a required column is absent.
- `parse_csv_row(row)`: Apply `PRICE_MAP`, `FIELD_MAP` and `BOOL_MAP` to one row, call `fix_product_data()` and return a `Product`.
- `parse_xml_product(elem)`: The same for one XML element. The map values are tag names.
- `check_columns(available, fold_case=True)`: Validate column names against `REQUIRED_COLUMNS` and `OPTIONAL_COLUMNS`.
- `check_xml_columns(elements)`: Run `check_columns()` on the child tags of the first product element, case-sensitive.
- `fix_product_data(data)`: Common clean-up before the `Product` is created (see "Data Conventions"). Override it for chain-specific fixes and call `super().fix_product_data(data)`.
- `parse_price(price_str, required=True)`: Parse a price string to a `Decimal` with 2 decimal places.
- `parse_bool(value)`: Parse values such as `dostupno`/`nedostupno` (see `TRUE_VALUES` and `FALSE_VALUES`). Unknown values return `None` and are logged once per value.
- `parse_anchor_date(value)`: Parse an anchor price date such as `2.5.2025`, `02.05.2025.` or `02052025` to an ISO date, or return `""`.
- `strip_diacritics(text)`: Remove diacritics from a string.
- `get_zip_contents(url, suffix)`: Download a ZIP and yield `(filename, bytes)` for each file whose name ends with `suffix`.
- `parse_index_for_zip(html_content)`: Return `{date: zip_url}` for the ZIP links that match `ZIP_DATE_PATTERN`.
- `is_retryable(err)`: Return whether another attempt at a failed request can succeed.
- `get_all_products(date)`: Main entry point; returns `list[Store]`. Each crawler must implement it (the base method raises `NotImplementedError`).
- `crawl(date)`: Call `get_all_products()`, log the store and price counts and the elapsed time, and log and re-raise errors. The `__main__` blocks use it; `crawl_chain()` calls `get_all_products()` directly.

Instance attributes: `self.client` (the `httpx.Client`), `self.blocked` (see `CrawlerBlocked` below) and `self.requests_made` (number of attempts sent, retries included).

#### Column Maps and Column Checks

`PRICE_MAP`, `FIELD_MAP` and `BOOL_MAP` map a field name to `(column_spec, is_required)`. A column spec is one column name, or a tuple or list of accepted names. The first name that is present in the file is used, even if its value in a row is empty. CSV column names are matched case-insensitively. XML tag names are matched case-sensitively.

The `is_required` value in the maps and `REQUIRED_COLUMNS` answer different questions:

- **`is_required` (row level)**: may an individual row leave the value empty? If a required price is empty or invalid, a required field is empty, or a required boolean is empty or unknown, the row raises `ValueError`. `parse_csv()` logs the row and skips it.
- **`REQUIRED_COLUMNS` (file level)**: must the file contain the column? A missing required column is a breaking format change: `check_columns()` raises `ValueError` and the file is abandoned. The caller logs the error.
- **`OPTIONAL_COLUMNS`**: columns the crawler expects but can work without, such as the ones that NN 101/2026 does not require. A missing optional column is logged as a warning and parsing continues.

`REQUIRED_COLUMNS` repeats column names from the maps on purpose. Fields such as `price` and `barcode` are row-optional in most crawlers, because `fix_product_data()` fills in a fallback. If the whole column disappears, the result would be wrong prices and synthetic barcodes, so the column is still required at file level. In `REQUIRED_COLUMNS` and `OPTIONAL_COLUMNS`, a tuple or list of names means that any one of the names is sufficient.

Where the checks run:

- `parse_csv()` calls `check_columns()` with the CSV header.
- XML crawlers call `check_xml_columns(product_elems)` once per file, before they parse the products (see `RibolaCrawler.parse_xml()`).
- XLSX crawlers that pass rows to `parse_csv_row()` (Žabac, Diskont Stanić) call `check_columns()` with the header row.
- Crawlers that parse files with their own code instead of the maps (dm, Djelo Vodice) call `check_columns()` themselves.

`BOOL_MAP` values are parsed with `parse_bool()`. The usual field is `available`.

The maps can also contain helper fields that are not on `Product` (for example `on_special` in Konzum's `BOOL_MAP`, `base_price` in Roto's `PRICE_MAP`). The crawler's `fix_product_data()` removes them with `data.pop()` and uses them there.

#### Anchor Price Date

`ANCHOR_PRICE_DATE` is the reference date of the anchor price, as the chain states it in the anchor price column header (for example, "Sidrena cijena na 2.5.2025" gives `datetime.date(2025, 5, 2)`). Set it per crawler from that header. The crawler does not detect it at runtime. Leave it `None` when the header does not state a date.

`fix_product_data()` sets `anchor_price_date` in this order:

1. A date that the chain publishes per product (map it to the `anchor_price_date` field in `FIELD_MAP`; it is parsed with `parse_anchor_date()`).
2. `ANCHOR_PRICE_DATE`.
3. `None`.

A row without an anchor price gets no date.

Since November 2026 several chains name the anchor price column "bazna cijena" (NN 132/2026). These headers do not state a date, so rows read from such a column must not get `ANCHOR_PRICE_DATE`:

- `RotoCrawler` maps "Bazna cijena" to a separate `base_price` field. Its `fix_product_data()` copies it to `anchor_price` and sets `anchor_price_date` to `None`.
- `BrankaCrawler` accepts `("SIDRENA_CIJENA_NA_02_05_25", "BAZNA_CIJENA")` for `anchor_price` and reads the per-product date from `DATUM_BAZNE_CIJENE` in its `parse_csv_row()`.
- Where the reference date differs per row and the file does not give it (Spar, KTC), `ANCHOR_PRICE_DATE` stays `None`.

#### Retries and Request Pacing

`fetch_text()`, `fetch_binary()` and `post_form()` send every request through the same retry logic:

- **Attempts**: `MAX_RETRIES` (default 6) is the total number of attempts per request, the first attempt included. The `attempts` argument overrides it for one call (Konzum uses `attempts=self.CSV_ATTEMPTS` and makes its own passes over the missing price lists).
- **What is retried**: transport errors (timeouts, connection resets), every 5xx status, and the statuses in `RETRY_STATUS` (408, 425, 429). Errors in `PERMANENT_ERRORS` (`httpx.UnsupportedProtocol`, `httpx.LocalProtocolError`, `httpx.TooManyRedirects`) and other 4xx statuses are not retried. 404 is not retried by default, because for most chains it means the file is not published yet. `KonzumCrawler` adds 404 to `RETRY_STATUS`.
- **Backoff**: the wait before the 2nd attempt is `RETRY_BACKOFF_BASE` seconds. It doubles per attempt, is capped at `RETRY_BACKOFF_MAX`, and is jittered by +/-50%.
- **429 and Retry-After**: the crawler waits at least the `Retry-After` time (in seconds or as an HTTP date) and holds off that host for that time. If `Retry-After` is longer than `RETRY_AFTER_MAX`, the crawler raises `CrawlerBlocked`.
- **Pacing**: `REQUEST_DELAY` is the minimum time between two requests to the same host. The default is 0. Chains that throttle or ban the crawler set it (`KonzumCrawler`: 1.0 s, `BosoCrawler`: 7.0 s). Pacing is per host. The wait before a retry is the larger of `REQUEST_DELAY` and the backoff; the two are not added.
- **Blocking**: a status in `BLOCKED_STATUS` (403) raises `CrawlerBlocked` at once, without a retry, and sets `self.blocked = True`. After that, every request raises `CrawlerBlocked` without contacting the server.

Crawlers that run their own retry passes call `is_retryable(err)` before they queue a URL again (see `KonzumCrawler.get_all_products()`).

Do not swallow `CrawlerBlocked` per store. There are two correct ways to handle it:

- Stop the loop and return the stores collected so far (see `LidlCrawler.get_all_products()`, `BosoCrawler.get_all_products()`). `crawl_chain()` sees `crawler.blocked`, logs an error that the chain is incomplete, and saves the collected stores.
- Let it propagate out of `get_all_products()` (Spar, Tommy, Dukat and Lorenco re-raise it). `crawl_chain()` then discards the run and writes no files for the chain.

## Data Models

### Product Model (`models.py`)

```python
class Product(BaseModel):
    # Required fields
    product: str          # Product name
    product_id: str       # Store-specific product ID
    brand: str           # Brand name
    quantity: str        # Amount (e.g., "500g", "1L")
    unit: str           # Unit of measure
    price: Decimal      # Current retail price
    unit_price: Decimal # Price per unit
    barcode: str        # EAN/barcode
    category: str       # Product category

    # Optional fields
    best_price_30: Optional[Decimal] = None      # Lowest price in 30 days
    special_price: Optional[Decimal] = None      # Promotional price
    available: Optional[bool] = None             # In stock; None if the chain does not publish it
    special_sale_type: Optional[str] = None      # Name of the special form of sale
    anchor_price: Optional[Decimal] = None       # Reference price (sidrena/bazna cijena)
    anchor_price_date: Optional[str] = None      # ISO date of the anchor price
    packaging: Optional[str] = None              # Packaging info
    initial_price: Optional[Decimal] = None      # Initial price for new products
    date_added: Optional[date] = None            # When product was added
```

`packaging`, `initial_price` and `date_added` are not written to the CSV output.

### Store Model (`models.py`)

```python
class Store(BaseModel):
    chain: str              # Chain identifier (lowercase)
    store_id: str          # Chain-specific store ID
    name: str              # Display name
    store_type: str        # "supermarket", "hipermarket", etc.
    city: str              # City name (normalized on output, see "City Names")
    street_address: str    # Street address
    zipcode: str = ""      # Postal code
    items: List[Product] = Field(default_factory=list)
```

## Implementation Patterns

The examples below are simplified from existing crawlers. Imports are omitted; `CrawlerBlocked` comes from `crawler.store.base`.

### 1. Index-Based Crawlers

Most common pattern for stores that provide index pages with links to individual store price lists:

```python
def get_all_products(self, date: datetime.date) -> list[Store]:
    # 1. Get the price list URLs for this date from the index page
    csv_links = self.get_index(date)

    stores = []
    for url in csv_links:
        try:
            # 2. Parse store info from URL/filename
            store = self.parse_store_info(url)

            # 3. Download and parse price data
            store.items = self.get_store_prices(url)

        except CrawlerBlocked as e:
            # Stop the chain and keep the stores collected so far
            logger.error(f"Stopping the crawl: {e}")
            break
        except Exception as e:
            # Skip this store, not the whole chain
            logger.error(f"Error processing {url}: {e}", exc_info=True)
            continue

        if not store.items:
            logger.warning(f"No products found in {url}, skipping")
            continue

        stores.append(store)

    return stores
```

**Examples**: Konzum, KTC, Metro, NTL, Lidl, Žabac, Brodokomerc, Trgovina Krk, Roto, Branka, Dukat, Lorenco, Jadranka Trgovina, Boso (Boso gets the per-store links from an AJAX endpoint with `post_form()`)

### 2. ZIP Archive Crawlers

For stores that provide ZIP files containing multiple CSV/XML files, the base class provides a `get_zip_contents()` generator to simplify extraction. The typical implementation pattern is (simplified from `EurospinCrawler`):

```python
def get_all_products(self, date: datetime.date) -> list[Store]:
    # 1. Find ZIP URL for the date
    zip_url = self.get_index(date)
    if not zip_url:
        return []

    stores = []
    # 2. Extract and process each file in the ZIP
    for filename, content in self.get_zip_contents(zip_url, ".csv"):
        try:
            store = self.parse_store_info(filename)
            store.items = self.parse_csv(content.decode("windows-1250"), delimiter=";")
        except Exception as e:
            logger.error(f"Error processing {filename}: {e}", exc_info=True)
            continue

        stores.append(store)

    return stores
```

`get_zip_contents()` downloads the ZIP to a temporary file with `fetch_binary()` and yields the name and content of each matching file. If one file in the ZIP cannot be read, it logs the error and continues with the next file.

A daily ZIP can contain more than one file per store. `PlodineCrawler.get_all_products()` reads the timestamp from each file name, skips files published on another date, and keeps the latest file per store. `TrgocentarCrawler.get_zip_stores()` also keeps the latest file per store.

**Note**: `StudenacCrawler` (`studenac.py`) overrides `get_zip_contents()`. It extracts the ZIP with the `unzip` command-line tool, because the file names in older archives use an encoding that `zipfile` does not convert. Since 17.11.2026 the Studenac ZIP contains inner ZIPs, which it reads with `zipfile`. Studenac's ZIP contains XML files rather than CSVs.

**Examples**: Plodine, Eurospin, Studenac, Bure, Trgocentar (since 17.11.2026)

### 3. API-Based Crawlers

For stores with JSON APIs (simplified from `SparCrawler`):

```python
def get_all_products(self, date: datetime.date) -> list[Store]:
    # 1. Fetch the JSON list of price files for the date: {filename: csv_url}
    csv_files = self.fetch_price_list_index(date)

    stores = []
    for filename, url in csv_files.items():
        store = self.parse_store_from_filename(filename)
        if not store:
            continue

        try:
            csv_content = self.fetch_text(url, ["iso-8859-2", "windows-1250"])
            store.items = self.parse_csv(csv_content, ";")
        except CrawlerBlocked:
            raise  # crawl_chain() discards the run
        except Exception as e:
            logger.error(f"Error processing {url}: {e}", exc_info=True)
            continue

        stores.append(store)

    return stores
```

**Examples**: Spar, Tommy, Kaufland (JSON file list linked from the index page), Diskont Stanić (Google Drive API; needs the `GOOGLE_API_KEY` environment variable)

### 4. Single-File Crawlers

For stores with global pricing (not per-store), simplified from `DmCrawler`:

```python
def get_all_products(self, date: datetime.date) -> list[Store]:
    # 1. Find the price file for the date (raises ValueError if there is none)
    content = self.fetch_text(self.INDEX_URL)
    url = self.find_price_list_url(content, date)

    # 2. Download and parse
    products = self.parse_csv_file(self.fetch_text(url, encodings=["utf-8-sig"]))

    # 3. Create single "global" store
    store = Store(
        chain=self.CHAIN,
        store_id="all",
        name="DM",
        store_type="store",
        city="",
        street_address="",
        items=products,
    )

    return [store]
```

**Examples**: DM

Lorenco and Jadranka Trgovina were single-file crawlers until November 2026. They now read one price list per store. Lorenco keeps the store ID `all` for its first store (PJ01), so its price history continues.

### 5. Excel-Based Crawlers

Some stores provide data in Excel format (.xlsx) instead of CSV or XML. The crawlers use `openpyxl` to read them.

The preferred approach is in `ZabacCrawler.parse_excel()` and `StanicCrawler.parse_excel()`: read the header row, call `check_columns()` with it, convert each row to a `{header: value}` dict, and pass the dict to `parse_csv_row()`. The maps and `fix_product_data()` then apply as for a CSV file.

`DmCrawler` and `DjeloVodiceCrawler` read XLSX files for dates before 17.11.2026 with their own parsers. Both chains publish CSV files since then.

**Examples**: Žabac, Diskont Stanić

### 6. XML-Based Crawlers

Some stores provide data as XML files (not inside ZIP archives). These crawlers fetch XML files directly and use XPath to extract product elements (simplified from `RibolaCrawler`):

```python
def get_all_products(self, date: datetime.date) -> list[Store]:
    # 1. Get XML file URLs for the date
    xml_urls = self.get_index_urls_for_date(date)

    stores = []
    for url in xml_urls:
        try:
            # 2. Fetch the XML; parse the store info and the products from it
            store = self.get_store_data(url)
        except Exception as e:
            logger.error(f"Error processing {url}: {e}", exc_info=True)
            continue

        if not store.items:
            continue

        stores.append(store)

    return stores
```

**Examples**: Ribola, Vrutak (XML is the fallback when a date has no CSV files), Trgocentar (dates before 17.11.2026). Bure and Studenac read XML files from a ZIP.

### 7. Additional considerations

Use `fetch_text()`, `fetch_binary()` and `post_form()` for all HTTP requests. They apply pacing, retries and block detection. `self.client` is the initialized `httpx.Client`, but a request sent directly through it skips all of these. No crawler uses `self.client` directly at present.

When parsing the HTML content, use the `BeautifulSoup` library (version 4) for parsing, and prefer CSS selectors for element selection,
like in this example:

```python
from bs4 import BeautifulSoup
...
soup = BeautifulSoup(content, "html.parser")
urls = []
csv_links = soup.select("a[href$='.csv']")

for link in csv_links:
    href = link.get("href")
    # we know href exists and is non-empty because of our selector
    urls.append(f"{self.BASE_URL}{href}")
```

## Common Implementation Steps

### 1. Store Information Parsing

Extract store details from filenames, URLs, or API responses:

```python
def parse_store_info(self, filename: str) -> Store:
    # Common patterns:
    # - Regex parsing of filenames
    # - URL parameter extraction
    # - API response field mapping

    # Example filename: "SUPERMARKET_ULICA_NAZIV_12345_GRAD_001_20250515.csv"
    pattern = r"([^_]+)_(.+)_(\d{5})_([^_]+)_(\d+)_.*\.csv"
    match = re.match(pattern, filename)
    if not match:
        raise ValueError(f"Invalid filename format: {filename}")

    store_type, address, zipcode, city, store_id = match.groups()
    return Store(
        chain=self.CHAIN,
        store_type=store_type.lower(),
        store_id=store_id,
        name=f"{self.CHAIN.capitalize()} {city}",
        street_address=address.replace("_", " ").title(),
        zipcode=zipcode,
        city=city,
        items=[],
    )
```

The output code normalizes the city name (see "City Names"), so the crawler can pass the city as the source spells it.

### 2. Price Data Parsing

For CSV files, call `parse_csv(content, delimiter)`. It does these steps:

1. Read the header and call `check_columns()`. A missing required column raises `ValueError`, and the caller abandons the file.
2. For each row, call `parse_csv_row()`. It applies `PRICE_MAP` (with `parse_price()`), `FIELD_MAP` (stripped text) and `BOOL_MAP` (with `parse_bool()`).
3. Call `fix_product_data()` and create the `Product`.
4. If a row raises an exception, log it and continue with the next row.

Put chain-specific fixes in a `fix_product_data()` override (simplified from `RotoCrawler`):

```python
def fix_product_data(self, data: dict) -> dict:
    # Remove the helper field before the common fixes
    base_price = data.pop("base_price", None)
    data = super().fix_product_data(data)

    # "Bazna cijena" states no date in its header
    if data.get("anchor_price") is None and base_price:
        data["anchor_price"] = base_price
        data["anchor_price_date"] = None

    return data
```

Override `parse_csv_row()` only when the maps cannot express the format. For example, `BrankaCrawler.parse_csv_row()` reads the per-product anchor date.

### 3. XML Data Processing

For XML-based stores, use the `parse_xml_product` method from `BaseCrawler`.

- Define `PRICE_MAP` and `FIELD_MAP` with XML tag names instead of CSV columns. Tag names are case-sensitive.
- Call `check_xml_columns(product_elems)` once per file, before the loop over the products, so that a format change abandons the file instead of failing each product.
- The base method uses simple XPath expressions (`<tag>/text()` relative to the product element). For more complex XML structures, you may need to override it.
- See `StudenacCrawler.parse_xml()` in `studenac.py` for store info parsing from XML.
- See `RibolaCrawler.parse_xml()` in `ribola.py` for a complete store and product parsing example.
- See `TrgocentarCrawler` in `trgocentar.py` for XML with filename-based store info and a known-cities list for address parsing.
- See `VrutakCrawler` in `vrutak.py` for CSV and XML URLs grouped by date from an HTML table, with the XML files as a fallback.

An XML field mapping would look like this, using tag names:
```python
FIELD_MAP = {
    "product": ("NazivProizvoda", True),  # Maps to <NazivProizvoda> tag
    "product_id": ("SifraProizvoda", True),
}
```

### 4. Date Pattern Matching

For index pages that link one ZIP file per date, set the `ZIP_DATE_PATTERN` class attribute. The pattern must have three groups: day, month and year, in that order.

```python
ZIP_DATE_PATTERN = re.compile(r".*_(\d{2})_(\d{2})_(\d{4})\.zip")
```

The `BaseCrawler` class provides a helper method, `parse_index_for_zip()`, which uses this pattern to return a dictionary of dates to URLs. For an example of how this is used, see the `get_index()` method in `PlodineCrawler` (`plodine.py`). Plodine is the only crawler that uses it at present. Most crawlers match the date in the file names with their own code (for example `MetroCrawler.get_index()` and `KauflandCrawler.get_index()`).

### 5. Error Handling and Logging

`BaseCrawler.crawl()` logs the start of the crawl, the number of stores and prices, and the elapsed time. It logs and re-raises errors. Crawlers do not need their own start and summary logging.

`crawl_chain()` in `crawler/crawl.py` calls `get_all_products()` and handles the result:

- If `get_all_products()` raises (`CrawlerBlocked` or any other exception), `crawl_chain()` logs the error and returns an empty result. It writes no files for the chain.
- If the crawler returned normally but `crawler.blocked` is set, it logs an error that the chain is incomplete and saves the stores collected before the block.
- If the crawler returned no stores, it logs `No stores imported for <chain> on <date>` and writes no files.

`crawl()` continues with the next chain in each of these cases. Because of this, `crawler.cli.crawl` exits with code 0 when a chain fails. Check the error messages in the log, or the row counts (see "Daily Runner").

## Data Conventions

These rules apply to all crawlers. Most of them are implemented in `BaseCrawler.fix_product_data()`.

### Prices

- A price of 0 means that the chain publishes no price for the row. It does not mean that the product is free.
- If `price` is missing or 0, `fix_product_data()` uses `special_price`, else `unit_price`. If all three are missing, the row fails.
- `special_price`, `best_price_30` and `anchor_price` with the value 0 become `None`.
- If `unit_price` is missing, it becomes `price`.
- Since 17.11.2026 some chains publish one price column and the name of the special form of sale. On sale rows, the published price is the sale price, so the crawler copies it to `special_price` (see `KonzumCrawler.fix_product_data()`, `PlodineCrawler.fix_product_data()`, `DmCrawler.parse_rows()`).
- `special_sale_type` is stripped. An empty value becomes `None`.

### Barcodes

- A barcode in scientific notation (for example `3,85889E+12`, from a spreadsheet export) is ignored, because it does not identify a product. The crawler logs a warning once per run and uses the chain product ID instead.
- An empty barcode becomes `<chain>:<product_id>`. Quotes are removed from barcodes.
- On output, `normalize_barcode()` from `common/barcodes.py` removes leading zeros from real EANs (8 or more digits). It does not change synthetic `<chain>:<product_id>` codes or GS1 restricted circulation numbers.

### Availability

- Map the availability column in `BOOL_MAP` as `available`. `parse_bool()` reads values such as `dostupno` as `True` and `nedostupno` as `False`.
- Keep unavailable rows. They stay in the output with `available` set to `False` (`0` in `prices.csv`).
- `None` means that the chain does not publish availability. Do not read it as "unavailable".

### Store failures

When one store fails after the retries (download or parse error), log the error and skip that store. Do not fail the whole chain. `CrawlerBlocked` is the exception to this rule (see "Retries and Request Pacing"). See `SparCrawler`, `TommyCrawler` and `DukatCrawler` for examples.

### Dates (rule for new crawlers)

`get_all_products(date)` must use `date` to select the files to read. If the source has no data for that date, return an empty list (or raise `ValueError`). Do not return the latest available file instead. Not all existing crawlers follow this rule yet; new crawlers must follow it. For examples, see `PlodineCrawler.get_all_products()`, `LidlCrawler.get_index()` and `MetroCrawler.get_index()`.

## Code Style and Conventions

### 1. Naming Conventions

- **Class names**: `ChainNameCrawler` (PascalCase)
- **File names**: `chain_name.py` (lowercase with underscores)
- **Chain identifiers**: `"chain_name"` (lowercase)
- **Methods**: `snake_case`
- **Constants**: `UPPER_CASE`

### 2. Method Organization

Standard method order in crawler implementations:

```python
class ChainCrawler(BaseCrawler):
    # 1. Class constants
    CHAIN = "chain"
    BASE_URL = "..."
    PRICE_MAP = {...}
    FIELD_MAP = {...}
    REQUIRED_COLUMNS = [...]
    OPTIONAL_COLUMNS = [...]

    # 2. Index/URL discovery methods
    def get_index(self, date): ...
    def parse_index(self, content): ...

    # 3. Store information parsing
    def parse_store_info(self, source): ...

    # 4. Price data fetching and parsing
    def get_store_prices(self, url): ...
    def parse_csv_row(self, row): ...  # if needed

    # 5. Chain-specific data fixing
    def fix_product_data(self, data): ...  # if needed

    # 6. Main entry point
    def get_all_products(self, date): ...
```

### 3. Error Handling

- Use `try/except` blocks for individual store processing
- Log warnings for non-critical errors (e.g., parsing single products)
- Log errors for store-level failures
- Continue processing remaining stores on individual failures
- Do not catch `CrawlerBlocked` per store: stop the loop and return the stores collected so far, or let it propagate
- Raise exceptions only for complete crawl failures (for example, no price list for the date)

### 4. Documentation

Each crawler should include:

```python
class ChainCrawler(BaseCrawler):
    """
    Crawler for Chain store prices.

    Brief description of the crawling approach and any special considerations.
    """

    def complex_method(self, param):
        """
        Method description.

        Args:
            param: Parameter description

        Returns:
            Return value description

        Raises:
            ValueError: When this specific error occurs
        """
```

Note: Docstrings should be concise but informative (one-liner is not enough), and added to all methods.

## Testing and Debugging

### Running Individual Crawlers

Each crawler includes a `__main__` block for testing:

```python
if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    crawler = ChainCrawler()
    stores = crawler.crawl(datetime.date.today())
    print(stores[0])
    print(stores[0].items[0])
```

After implementing the crawler, you can run it directly to test functionality and inspect output. From the project root, run:

```bash
uv run -m crawler.store.<crawler_module>
```

To test the integration with the rest of the crawler, check that it is registered with:

```bash
uv run -m crawler.cli.crawl -l
```

Then crawl one chain for one date and inspect the CSV files:

```bash
uv run -m crawler.cli.crawl -c <chain> -d YYYY-MM-DD -z false -v info /path/to/output/
```

### Automated Tests

Run the crawler tests with:

```bash
uv run pytest tests/crawler
```

- `tests/crawler/test_daily.py` checks that every chain in `CRAWLERS` has an `EXPECTED` entry in `crawler/cli/daily.py`.
- `tests/crawler/store/test_retry.py` covers retries, pacing and `CrawlerBlocked`.
- `tests/crawler/store/test_anchor_date.py` covers the anchor price date rules.
- `tests/crawler/store/test_output.py` and `tests/crawler/store/test_cities.py` cover the CSV output and the city names.
- `tests/crawler/store/test_djelo_vodice.py` is an example of tests for one crawler: column checks and index parsing without network access.

### Common Debugging Patterns

1. **Enable debug logging**: `logging.basicConfig(level=logging.DEBUG)`, or `-v debug` on the command line. Debug logs show each failed attempt and each pacing wait.
2. **Check field mappings**: Verify CSV/XML column names match expectations. A `Missing required column(s)` error lists the columns found in the file.
3. **Test price parsing**: Use `parse_price()` method directly
4. **Inspect raw data**: Print fetched content before parsing
5. **Validate URLs**: Check if generated URLs are accessible

## Troubleshooting

### Common Issues

1. **Encoding Problems**
   - Try multiple encodings: `self.fetch_text(url, encodings=["windows-1250", "utf-8"])`
   - Use `prefix` to choose between encodings that both decode without error. See `SparCrawler.get_all_products()` (`CSV_PREFIX`).
   - `VrutakCrawler.decode_csv()` chooses between windows-1250 and CP852 by counting Croatian letters in the decoded text.

2. **Date Format Issues**
   - Check that your `ZIP_DATE_PATTERN` matches the date format in the URLs.
   - Include the available dates in the error message to debug mismatches. See `LidlCrawler.get_index()` for an example.

3. **Store Parsing Failures**
   - Test regex patterns with actual filenames from the website.
   - For complex address parsing, see `KauflandCrawler.parse_store_info()`.

4. **Empty Results**
   - Verify that the `date` parameter is handled correctly by the crawler and the website.
   - Check if the store provides historical data or only current prices. Some existing crawlers are date-agnostic; new crawlers must not be (see "Dates").

5. **HTTP 403 or 429 Errors**
   - A 403, or a 429 with a `Retry-After` longer than `RETRY_AFTER_MAX`, raises `CrawlerBlocked`. Increase `REQUEST_DELAY` for the chain (see the comments in `BosoCrawler` and `KonzumCrawler`).

### Debugging Approach

1. **Test URL fetching**: `content = crawler.fetch_text(url)`
2. **Test parsing**: `products = crawler.parse_csv(content)`
3. **Test individual components** before combining them in `get_all_products`.
4. **Enable debug logging** at the top of your `if __name__ == "__main__"` block: `logging.basicConfig(level=logging.DEBUG)`

## Output Format

### CSV Structure

`save_chain()` writes three CSV files per chain, in `<output_path>/<YYYY-MM-DD>/<chain>/`:

1. **stores.csv**: Store information
   - `store_id`, `type`, `address`, `city`, `zipcode`

2. **products.csv**: Unique products (one row per `product_id`; the first occurrence is used)
   - `product_id`, `barcode`, `name`, `brand`, `category`, `unit`, `quantity`

3. **prices.csv**: Price data linking stores and products
   - `store_id`, `product_id`, `price`, `unit_price`, `best_price_30`, `anchor_price`, `special_price`, `available`, `special_sale_type`, `anchor_price_date`

Details:

- `None` values are written as empty strings. `available` is `1`, `0`, or empty.
- Whitespace inside values is collapsed to one space, so each row is one line.
- Rows are sorted by `store_id` and `product_id`.
- If a list is empty, `save_csv()` logs a warning and does not write that file.

### City Names (`cities.py`)

`transform_products()` passes each store city through `normalize_city()`. The function makes a lookup key (diacritics removed, lowercase, whitespace and `- _ . ( )` collapsed to one space) and looks it up in `enrichment/cities.csv` (columns `key,canonical`). A known city gets its canonical name. An empty city stays empty.

A city that is not in the map is title-cased (connector words such as "na" and "i" stay lowercase; no diacritics are added) and logged at INFO level as `Unmapped city ...`. To fix it, add a row to `enrichment/cities.csv`. The default log level of `crawler.cli.crawl` is `warning`, so use `-v info` to see these messages.

### ZIP Archive

Unless `-z false` is given, `crawl()` copies `archive-info.txt` into the date directory and creates `<output_path>/<YYYY-MM-DD>.zip`. The archive contains:
- `<chain>/stores.csv`, `<chain>/products.csv` and `<chain>/prices.csv` for each chain with data
- `archive-info.txt` with format documentation (in Croatian) and the list of chains

## Command-Line Tools

### Crawler CLI (`crawler/cli/crawl.py`)

```bash
uv run -m crawler.cli.crawl [-d DATE] [-c CHAIN] [-l] [-z {true,false}] [-v LEVEL] [output_path]
```

- `-d, --date`: date to crawl, `YYYY-MM-DD` (default: today)
- `-c, --chain`: comma-separated list of chains (default: all). An unknown chain name is an error.
- `-l, --list`: list the supported chains and exit (`output_path` is not required)
- `-z, --create-zip {true,false}`: create the ZIP after the crawl (default: `true`)
- `-v, --verbose {debug,info,warning,error,critical}`: log level (default: `warning`). Only messages from `crawler.*` loggers are shown.

The chains run one after another in one process. A failed chain does not stop the other chains and does not change the exit code (see "Error Handling and Logging").

### Daily Runner (`crawler/cli/daily.py`)

`crawler.cli.daily` crawls one date with one `crawler.cli.crawl` process per chain, all at the same time. It has three commands: `run`, `status` and `cleanup`.

- The data for a date stays in `<root>/data/<DATE>/` between runs. Each `run` crawls only the chains whose data is missing or incomplete, plus the chains given with `--force`.
- A chain is complete when its store, product and price counts are each at least 90% (`THRESHOLD`) of its entry in `EXPECTED`.
- A new attempt replaces the chain's data only if it has more data (prices first, then stores, then products). An attempt with a non-zero exit code is discarded. A process that runs longer than `CHAIN_TIMEOUT` (3 hours) is killed.
- When the data changed, `run` publishes `<DATE>.zip` (in `<root>` or in `--archive-dir`). It exits with code 1 if no ZIP exists after the run.

Every chain in `CRAWLERS` needs an entry in `EXPECTED`: (stores, products, prices) from a known-good run. Without it, `run` and `status` stop with an error, and `tests/crawler/test_daily.py` fails. Update the entry by hand when a chain opens or closes stores.

The README section "Dnevno preuzimanje" (in Croatian) describes the usage.

## Common Challenges and Solutions

### 1. Character Encoding

Many Croatian sites use Windows-1250 encoding:

```python
content = self.fetch_text(url, encodings=["windows-1250", "utf-8"])
```

### 2. Price Parsing

Handle various price formats:

```python
# Base class handles: "1,50€", "1.50", ",50", "1.200,50", "1,200.50", "12,99 EUR"
price = self.parse_price(price_str, required=False)
```

- When a string contains both `,` and `.`, the first one is the thousands separator.
- A string without digits (for example `-`) counts as empty.
- `parse_price()` does not handle a space as the thousands separator (`"1 200,50"`): it logs a warning and returns `None` (or raises when `required=True`).
- The default is `required=True`, which raises `ValueError` for an empty or invalid price.

### 3. Date Extraction

Use regex patterns for date extraction:

```python
ZIP_DATE_PATTERN = re.compile(r".*_(\d{2})_(\d{2})_(\d{4})\.zip")
```

### 4. Store Address Parsing

Handle inconsistent address formats:

```python
def parse_address(self, address_str):
    # Try known city patterns first
    for city in self.CITIES:
        if address_str.endswith(city):
            street = address_str[:-len(city)].strip()
            return street, city

    # Fallback to regex or other methods
    return address_str, ""
```

`KauflandCrawler` and `TrgocentarCrawler` keep a `CITIES` list for this purpose.

## Adding a New Crawler

### Step-by-Step Guide

1. **Create new file**: `crawler/store/new_chain.py`

2. **Implement basic structure**:
```python
import datetime
import logging
from crawler.store.base import BaseCrawler, CrawlerBlocked
from crawler.store.models import Product, Store

logger = logging.getLogger(__name__)

class NewChainCrawler(BaseCrawler):
    CHAIN = "new_chain"
    BASE_URL = "https://www.newchain.hr"

    # Only if the anchor price header states a date
    ANCHOR_PRICE_DATE = datetime.date(2025, 5, 2)

    PRICE_MAP = {
        "price": ("Price Column", True),
        "unit_price": ("Unit Price Column", True),
        # ... other mappings
    }

    FIELD_MAP = {
        "product": ("Product Name Column", True),
        "product_id": ("Product ID Column", True),
        # ... other mappings
    }

    BOOL_MAP = {
        "available": ("Availability Column", False),
    }

    REQUIRED_COLUMNS = [
        "Price Column",
        "Unit Price Column",
        "Product Name Column",
        "Product ID Column",
        # ... other columns the file must have
    ]

    OPTIONAL_COLUMNS = [
        "Availability Column",
        # ... columns the crawler can work without
    ]

    def get_all_products(self, date: datetime.date) -> list[Store]:
        # Select the files for `date`; return [] if there are none
        pass
```

3. **Implement required methods** based on the chain's data structure. Follow the "Data Conventions" section, in particular the rule for dates.

4. **Test the implementation**:
```python
if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    crawler = NewChainCrawler()
    stores = crawler.crawl(datetime.date.today())
    print(f"Found {len(stores)} stores")
```

5. **Add to crawler registry** in `crawler/crawl.py`:

To register the new crawler, add it to the `CRAWLERS` dictionary in `crawler/crawl.py`:

```python
from crawler.store.new_chain import NewChainCrawler
...
CRAWLERS = {
    # ... existing chains
    NewChainCrawler.CHAIN: NewChainCrawler,
}
```

6. **Add an `EXPECTED` entry** in `crawler/cli/daily.py`, with the (stores, products, prices) counts from a good run. `tests/crawler/test_daily.py` fails without it.

7. **List the chain** in `crawler/store/archive-info.txt` (the list of chains in the archive) and in the list of supported chains in `README.md`.

8. **Run the tests**: `uv run pytest tests/crawler`

### Pre-Implementation Checklist

- [ ] Understand the chain's price list structure (CSV/XML/XLSX/JSON)
- [ ] Identify how to get the index/list of stores
- [ ] Determine date format used in URLs/filenames
- [ ] Map CSV/XML columns to our data model
- [ ] Decide which columns are required (`REQUIRED_COLUMNS`) and which are optional (`OPTIONAL_COLUMNS`)
- [ ] Check if the anchor price header states a date (`ANCHOR_PRICE_DATE`), or if the file gives a date per product
- [ ] Check if the file has an availability column (`BOOL_MAP`) and a special sale type column
- [ ] Identify store information location (filename, URL, API)
- [ ] Check character encoding requirements
- [ ] Test with different dates to ensure robustness
- [ ] Determine if the site provides historical data or only current prices
- [ ] Check if ZIP files require special handling (see `StudenacCrawler`)
- [ ] Verify date format in URLs matches your `ZIP_DATE_PATTERN`
- [ ] Test with both available and unavailable dates; an unavailable date must return no data
- [ ] Check if the site uses non-standard formats (Excel, XML, JSON API)
- [ ] Check if the site throttles or blocks fast requests (`REQUEST_DELAY`)
