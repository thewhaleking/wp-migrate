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
- [`PyMySQL`](https://pypi.org/project/PyMySQL/) — needed for `search-replace`, and for
  `import-db` / `migrate` **when `--table-prefix` is used**. Install via the `db` extra:
  `pip install ".[db]"` (or `pip install pymysql`).
- The **`mysql`** client binary on `PATH` (for `import-db` / `migrate`).

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

The dump is streamed line-by-line into `mysql` (never split into statements in
Python), and two All-in-One WP Migration export quirks are repaired on the fly so
a raw `mysql` import doesn't choke:

- **`utf8mb4` connection** — the AIO dump carries no `SET NAMES`, so the client is
  forced to `utf8mb4`. Without this, any 4-byte character (emoji like 🛒) fails
  with `ERROR 1366 Incorrect string value`.
- **Empty `0x` blob literals** — AIO emits a bare `0x` for an empty `BLOB` (common
  in Wordfence's `wfConfig`), which MySQL rejects (`Unknown column '0x'`). These
  are rewritten to `''`, string-aware so real hex literals and `0x` inside string
  data are never touched.

#### Rewriting the table prefix (`--table-prefix`)

An AIO dump names every table with the placeholder `SERVMASK_PREFIX_`. By default
the tables are imported with that literal prefix, and you'd set
`$table_prefix = 'SERVMASK_PREFIX_';` in `wp-config.php`. To get clean `wp_` tables
instead:

```bash
python3 wpress_migrate.py import-db ./extracted/database.sql \
    --db wp_target --user wp --password secret --table-prefix wp_
```

This rewrites the placeholder in two passes: **table identifiers** during the
import stream, and the prefix **embedded in row data** (the `…_capabilities`
usermeta keys, the `…_user_roles` option, and any serialized blobs) afterward via
the serialization-safe engine, so byte-length prefixes stay correct. Requires
`PyMySQL`. Import into a **freshly created database** so the rewritten `DROP TABLE`
statements don't leave orphan tables behind.

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
    --old-url https://old-site.com --new-url https://new-site.com \
    --table-prefix wp_
```

`--table-prefix` is optional here too; omit it to keep the `SERVMASK_PREFIX_`
tables. Import into a freshly created, empty database.

### Database connection options

`--db --user --password` are required for the DB subcommands. Optional:
`--host` (default `localhost`), `--port` (default `3306`), `--socket`
(a Unix socket path, overrides host/port).

## After migrating (manual steps)

1. Copy `extracted/uploads`, `extracted/themes`, `extracted/plugins` into your
   WordPress `wp-content/` (see [Troubleshooting](#troubleshooting) for the exact
   commands and the permissions step — this is the most common thing people miss).
2. Set `$table_prefix` in `wp-config.php` to match the imported tables — `'wp_'`
   if you used `--table-prefix wp_`, otherwise `'SERVMASK_PREFIX_'`.
3. Flush permalinks: `wp rewrite flush --hard` (or **Settings → Permalinks → Save**).

## Troubleshooting

`import-db` only loads the **database**. The `wp-content` **files** (themes,
plugins, uploads) are a separate manual copy — skipping it is the usual cause of a
blank front end after a "successful" import.

### Copy the `wp-content` files into the live install

First find the real WordPress root. `wp-config.php` is often **one level above**
the web root, so don't assume its directory is the install:

```bash
sudo find / -name wp-config.php 2>/dev/null          # the config (may be above the root)
find / -type d -name wp-content 2>/dev/null           # the live wp-content WP actually loads
```

For example, with `wp-config.php` at `/var/www/wp-config.php` the install is
usually `/var/www/html/` and the live folder is `/var/www/html/wp-content/`. Copy
into **that** one (using `cp -a` to preserve everything; the trailing `/.` copies
directory contents):

```bash
SRC=~/wpress-extracted
sudo cp -a "$SRC"/themes/.  /var/www/html/wp-content/themes/
sudo cp -a "$SRC"/plugins/. /var/www/html/wp-content/plugins/
sudo cp -a "$SRC"/uploads/. /var/www/html/wp-content/uploads/
```

### Fix ownership and permissions

Files copied as your shell user won't be readable/writable by the web server.
Give them to the web server user (`www-data` on Debian/Ubuntu Apache; `nginx` or
`apache` elsewhere):

```bash
sudo chown -R www-data:www-data /var/www/html/wp-content
sudo find /var/www/html/wp-content -type d -exec chmod 755 {} \;
sudo find /var/www/html/wp-content -type f -exec chmod 644 {} \;
```

### Front end is blank or "There has been a critical error"

Check what the server actually returns (this bypasses your browser, which **caches
301 redirects** and will keep sending you to the old domain even after a fix):

```bash
curl -sI http://YOUR_SERVER_IP        # status code + any Location: redirect
curl -s  http://YOUR_SERVER_IP | head -40
```

To see the real PHP error, in `wp-config.php` **above**
`require_once ABSPATH . 'wp-settings.php';`:

```php
define('WP_DEBUG', true);
define('WP_DEBUG_DISPLAY', true);
define('WP_DISABLE_FATAL_ERROR_HANDLER', true);   // shows the fatal instead of the generic page
```

Common causes, in order of likelihood:

- **`301` redirect to the old domain** — `home`/`siteurl` still point there. For a
  temporary preview without touching data, override in `wp-config.php`:
  ```php
  define('WP_HOME',    'http://YOUR_SERVER_IP');
  define('WP_SITEURL', 'http://YOUR_SERVER_IP');
  ```
  When the real domain is ready, remove these and run `search-replace OLD NEW`.
- **`200` with an empty body / critical error — missing theme.** The active theme's
  folder isn't in `wp-content/themes/` (files not copied), or the `template` /
  `stylesheet` options are empty/wrong. Confirm and set them:
  ```sql
  SELECT option_name, option_value FROM `wp_options`
    WHERE option_name IN ('template','stylesheet');
  UPDATE `wp_options` SET option_value = 'your-theme-slug'
    WHERE option_name IN ('template','stylesheet');
  ```
- **Critical error right after activating plugins — a plugin fatal.** Often an
  older plugin/theme that worked on the source's PHP 7.x but throws on a newer
  PHP 8.x (e.g. `Undefined constant`). The error log names the file:
  ```bash
  sudo tail -50 /var/log/apache2/error.log
  ```
  Disable the offending plugin by renaming its folder (WordPress auto-deactivates
  a plugin whose files vanish), or disable all of them and re-enable one by one:
  ```bash
  sudo mv /var/www/html/wp-content/plugins/<name> /var/www/html/wp-content/<name>.off
  ```
  ```sql
  UPDATE `wp_options` SET option_value = 'a:0:{}' WHERE option_name = 'active_plugins';
  ```
  If a theme/plugin is genuinely incompatible with PHP 8, either patch the offending
  lines or run the site on the PHP version the source used.

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
many-files-one-dir / path-traversal rejection), serialization-safe replacement
across length-changing edits, embedded `";`, nested arrays, `O:` objects,
double-serialization, and plain-text fallback, plus the SQL stream repairs
(empty-`0x` literals and `--table-prefix` identifier/row-data rewriting).

## Safety notes

- **Back up the target database** before running `search-replace` (it mutates
  rows in place). Use `--dry-run` first.
- `extract` refuses any archive entry whose path would escape the output
  directory (path-traversal guard).
