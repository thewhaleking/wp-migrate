# wp-migrate

Restore an [All-in-One WP Migration](https://wordpress.org/plugins/all-in-one-wp-migration/)
`.wpress` backup **without** the plugin's paid Unlimited Extension.

The free plugin caps restores at **512 MB** and blocks the WP-CLI import path too,
which makes a multi-gigabyte backup impossible to restore through the official
tooling. `wp-migrate` reads the `.wpress` format natively and does the three
things a restore actually needs:

1. **Extract** — parses the `.wpress` binary format and streams every file to
   disk, so a 14 GB archive never lands in memory.
2. **Import** — streams the bundled `database.sql` into MySQL via the `mysql`
   client (no fragile in-Python statement splitting).
3. **Search-replace** — rewrites the old site URL across every table in a way
   that **respects PHP serialization**, so widgets, theme mods, and plugin
   settings don't break.

> **What an AIO backup contains:** only the database plus the `wp-content`
> directories (`uploads`, `themes`, `plugins`) and a little metadata — **not**
> WordPress core. You restore into an existing WordPress install.

## Why the serialization-safe replace matters

WordPress stores a lot of settings as PHP-serialized blobs, which encode the
**byte length** of each string: `s:5:"hello";`. A naïve `sed`/find-and-replace
that changes `http://old.example.com` → `https://new-domain.io` leaves the
declared length wrong, and PHP then refuses to unserialize the value — silently
breaking widgets, menus, and plugin config.

`wp-migrate` walks the serialized structure by its declared lengths, replaces
inside string payloads, and re-emits each `s:<len>:"…"` with a **recomputed**
length. It handles nested arrays, `O:` objects, double-serialized strings, and
falls back to a plain byte replacement for non-serialized cells. The replacer is
validated in the test suite against [`phpserialize`](https://pypi.org/project/phpserialize/)
as an independent oracle.

## Requirements

- Python **3.10+**
- [`PyMySQL`](https://pypi.org/project/PyMySQL/) — `pip install pymysql` (only for `import-db` / `search-replace` / `migrate`)
- The **`mysql`** client binary on `PATH` (only for `import-db` / `migrate`)

`extract` has no dependencies beyond the Python standard library.

## Usage

The tool is a single-file CLI. Run any subcommand with `-h` for its full options.

### Extract

```bash
python3 wpress_migrate.py extract backup.wpress -o ./extracted
```

Shows a live progress bar. Add `-v`/`--verbose` to also list each file as it's
written.

### Import the database

```bash
python3 wpress_migrate.py import-db ./extracted/database.sql \
    --db wp_target --user wp --password secret
```

### Serialization-safe search-replace

```bash
python3 wpress_migrate.py search-replace \
    --db wp_target --user wp --password secret \
    https://old-site.com https://new-site.com --dry-run
```

`--dry-run` reports what would change without writing. Drop it to apply.

### Migrate (all three in one)

```bash
python3 wpress_migrate.py migrate backup.wpress -o ./extracted \
    --db wp_target --user wp --password secret \
    --old-url https://old-site.com --new-url https://new-site.com
```

### Database connection options

`--db --user --password` are required for the DB subcommands. Optional:
`--host` (default `localhost`), `--port` (default `3306`), `--socket`
(a Unix socket path, overrides host/port).

## After migrating (manual steps)

1. Copy `extracted/uploads`, `extracted/themes`, `extracted/plugins` into your
   WordPress `wp-content/`.
2. Set `$table_prefix` in `wp-config.php` to match the imported tables.
3. Flush permalinks: `wp rewrite flush --hard` (or **Settings → Permalinks → Save**).

## Performance

The extractor uses 1 MiB streaming chunks and avoids redundant per-file
syscalls. Benchmarked against the popular [`wpress-extract`](https://github.com/ofhouse/wpress-extract)
npm package on the same archives (byte-identical output):

| Archive profile        | wp-migrate | wpress-extract (npm) |
| ---------------------- | ---------- | -------------------- |
| 3 GB, large files      | ~1 s       | ~79 s (**~76× slower**) |
| 1 GB, ~97k tiny files  | ~16 s      | ~33 s (**~2× slower**)  |

(The npm tool reads in 512-byte chunks; `wp-migrate` is bounded by the storage
device, not the interpreter.)

## Testing

```bash
pip install phpserialize   # test-only oracle
python3 test_wpress.py
```

The suite covers archive round-trips (binary / multi-chunk / zero-length /
many-files-one-dir / path-traversal rejection) and serialization-safe replacement
across length-changing edits, embedded `";`, nested arrays, `O:` objects,
double-serialization, and plain-text fallback.

## Safety notes

- **Back up the target database** before running `search-replace` (it mutates
  rows in place). Use `--dry-run` first.
- `extract` refuses any archive entry whose path would escape the output
  directory (path-traversal guard).
