# Invoice Reader

Windows 11 desktop app that reads electrical-supply invoices (PDF or image), stores every line item by
item # / manufacturer / product name in SQLite, and tracks each product's price history over time.

## Native C++ app (Windows)

The native C++ implementation is in `cpp/`. It uses Win32 for the desktop UI, Windows OCR/PDF APIs to read
documents, and the Windows SQLite library. It opens the same `%LOCALAPPDATA%\InvoiceReader\invoices.db`
database as the Python app, so product and invoice history is shared.

Build from a Visual Studio Developer PowerShell or Developer Command Prompt with the **Desktop development
with C++** workload and a Windows 10/11 SDK installed:

```powershell
cmake -S cpp -B cpp/build -A x64
cmake --build cpp/build --config Release
ctest --test-dir cpp/build -C Release --output-on-failure
.\cpp\build\Release\invoice-reader.exe
```

The C++ app currently includes the dashboard, product and invoice lists, PDF/image OCR imports, a review
queue, and SQLite persistence against the existing schema. It is an initial native port; the Python app
still provides folder watching, tray/autostart behavior, advanced invoice parsing, editable review rows,
product editing/history charts, CSV exports, and database backup.

## Python app

The Python version remains available and supports the full feature set below.

Double-click **Invoice Reader.bat** (finds an installed Python 3 and creates the Python environment on first run), or:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\python main.py            # --minimized starts in the tray; file paths import immediately
```

## Windows installer

On the build computer, install Inno Setup 6 and Python with pip available as `python`, then run:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File ".\installer\build-installer.ps1"
```

The build downloads and SHA-256 verifies the official Python 3.14.10 x64 embeddable runtime, gathers
all runtime dependencies, smoke-tests them, and compresses everything into `dist\InvoiceReaderSetup.exe`.
Only the build computer needs internet access.

The single EXE installs fully offline for the current Windows user and adds Start Menu and desktop
shortcuts. Its private Python and packages live in the app's `runtime` folder; it does not use or change
other Python installations, PATH, or file associations. The runtime uses isolated, relative import paths
so the installation can be placed in any folder. This is an x64 build for 64-bit Windows (including
Windows 11 ARM64 through x64 emulation). Uninstalling leaves invoice data in
`%LOCALAPPDATA%\InvoiceReader` untouched.

When the app runs from the source folder (with Git installed), **Settings > Updates** can pull the
latest code from the Git remote (and refresh the Python packages), and rebuild
`dist\InvoiceReaderSetup.exe` with one click - both with live build output. Installed copies update by
running the newest `InvoiceReaderSetup.exe`.

## How it works

- **Watch folder** - defaults to your Downloads folder. Any new PDF/PNG/JPG/TIFF that finishes downloading
  is read automatically (partial downloads like `.crdownload` are ignored). Change it on the Dashboard or
  in Settings; "Scan existing files in folder now" picks up files that were already there.
- **Reading** - digital PDFs use their embedded text; scanned PDFs and images use the built-in Windows OCR.
  Every line is checked with `qty x price / per = extended` (E = each, C = per 100, M = per 1000).
- **Import queue** - each file is shown next to the extracted lines for review. Fix anything, then
  *Save to database*. With *Save invoices automatically when every line checks out* on (Settings),
  invoices whose math fully checks out are saved without review.
- **Vendors** - detected from the letterhead (OCR of the logo if needed). If you correct a vendor while
  reviewing, the app remembers it for that vendor's future invoices. Your own company name
  (Settings > *Your company name(s)*, default "Midstate Electric") is never picked as the vendor.
- **Products** - matched by item # (case, spaces, punctuation and OCR O/0 mix-ups are ignored). Turn on
  Settings > *Match products by item # and manufacturer* to keep the same catalog # from different
  manufacturers separate; editing a product's manufacturer to match an existing one merges their history.
  The Products page shows last / previous / low / high / average unit cost, a price chart and every purchase.
- **Correct saved products** - select a product and choose *Edit details* to change its item #,
  manufacturer, name, category or notes. Changing its item # / manufacturer to an existing product
  combines their history. To correct just one line, select it in the invoice's line items or a product's
  purchase history and choose *Save selected line/purchase as...*. Choose an existing product or enter
  a new item # and name; the original invoice description, quantity and price are preserved.
- **EMT sizes** - recognizable 10-foot EMT conduit descriptions such as `1inch X10 EMT` and
  `2" X 10FT` are matched by diameter, even if both were labeled `EMT2`. The first startup after this
  update also separates recognizable previously saved EMT entries; ambiguous descriptions and
  fittings are left alone for manual correction.
- **Exports** - Products and full price history to CSV; database backup from Settings.

Data lives in `%LOCALAPPDATA%\InvoiceReader` (`invoices.db`, `archive\<year>\<vendor>\` copies of every
imported file, and `logs\`). Set `INVOICE_READER_DATA` to use another folder.

## Remote database (MySQL / MariaDB)

By default everything is stored in the local SQLite file. To share one live database between several PCs
(and let other apps read it), point the app at a MySQL or MariaDB server from
**Settings > Database**: choose *MySQL / MariaDB server*, enter the host, database name and credentials,
*Test connection*, then *Save database settings* and restart the app. Tables are created automatically on
first connect, and *Copy local data to server* moves your existing local invoices, products and price
history into an empty server database.

Create the database and app user on the server first:

```sql
CREATE DATABASE invoice_reader CHARACTER SET utf8mb4;
CREATE USER 'invoice_app'@'%' IDENTIFIED BY '...';
GRANT ALL PRIVILEGES ON invoice_reader.* TO 'invoice_app'@'%';
```

For other applications (e.g. a Laravel app) that only need to read prices and products, use a
read-only account instead:

```sql
CREATE USER 'invoice_ro'@'%' IDENTIFIED BY '...';
GRANT SELECT ON invoice_reader.* TO 'invoice_ro'@'%';
```

Connection settings are stored in `database.json` in the data folder (the password is stored in plain
text, so use a dedicated least-privilege account). Notes: file backup from Settings applies to the local
SQLite file only - back up a server database with the server's tools (e.g. `mysqldump`); archived invoice
files stay on the PC that imported them, so the original document preview is available there.

Adding another engine later means implementing one more `Backend` subclass in
`invoice_reader/backends.py` (connection, SQL-dialect translation, and schema script).

## Tests

```powershell
.\.venv\Scripts\python -m pytest tests -q
```

`tests\fixtures` holds real Electrical Wholesalers documents used as parser regression tests.
