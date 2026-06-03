#!/usr/bin/env python3
"""
wpress-migrate — extract an All-in-One WP Migration .wpress archive, import its
database, and run a serialization-safe URL/string replacement, without the
plugin's paid Unlimited Extension.

Three things this does that a naive script can't:

  1. Parses the .wpress binary format natively and streams files to disk, so a
     14 GB archive never lands in memory.
  2. Imports the bundled database.sql via the `mysql` client (streamed).
  3. Rewrites the old site URL (or any string) across every table in a way that
     respects PHP serialization — recomputing `s:<len>:"..."` byte-length
     prefixes so widgets, theme mods, and plugin settings don't break. A plain
     sed/replace corrupts these; this does not.

Dependencies: Python 3.8+, PyMySQL (`pip install pymysql`), and the `mysql`
client binary on PATH (only for `import-db` / `migrate`).

Usage:
    wpress_migrate.py extract        ARCHIVE.wpress -o OUTDIR
    wpress_migrate.py import-db      OUTDIR/database.sql --db NAME --user U --password P
    wpress_migrate.py search-replace --db NAME --user U --password P OLD NEW [--dry-run]
    wpress_migrate.py migrate        ARCHIVE.wpress -o OUTDIR --db NAME --user U --password P \\
                                     --old-url https://old.com --new-url https://new.com

Run any subcommand with -h for its full options.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from dataclasses import dataclass

# ----------------------------------------------------------------------------
# .wpress archive format
#
# Header is a fixed 4377-byte block, zero-padded, big-endian field order:
#   name   [0:255]    filename, no path
#   size   [255:269]  content length, ASCII decimal
#   mtime  [269:281]  unix mtime, ASCII decimal
#   prefix [281:4377] directory path, no trailing slash
# A header block of all-zero bytes marks EOF. File content (size bytes) follows
# each header inline. (Verified against github.com/yani-/wpress.)
# ----------------------------------------------------------------------------

HEADER_SIZE = 4377
NAME = slice(0, 255)
SIZE = slice(255, 269)
MTIME = slice(269, 281)
PREFIX = slice(281, 4377)
EOF_BLOCK = b"\x00" * HEADER_SIZE
READ_CHUNK = 1024 * 1024  # 1 MiB streaming chunk


@dataclass
class Entry:
    name: str
    size: int
    prefix: str

    @property
    def relpath(self) -> str:
        # prefix is relative to the WP content root; "." means archive root.
        if self.prefix in ("", "."):
            return self.name
        return os.path.join(self.prefix, self.name)


def _parse_header(block: bytes) -> Entry | None:
    """Return an Entry for a header block, or None at EOF."""
    if len(block) < HEADER_SIZE or block == EOF_BLOCK:
        return None
    name = block[NAME].rstrip(b"\x00").decode("utf-8", "surrogateescape")
    size_raw = block[SIZE].rstrip(b"\x00")
    size = int(size_raw) if size_raw else 0
    prefix = block[PREFIX].rstrip(b"\x00").decode("utf-8", "surrogateescape")
    return Entry(name=name, size=size, prefix=prefix)


class _Progress:
    """Minimal carriage-return progress bar on stderr.

    Self-disables when stderr isn't a TTY (pipes, CI, the test suite) so it never
    pollutes captured output. Updates are throttled to ~10/s so a multi-GB file
    doesn't flood the terminal. `log()` prints a line cleanly above the bar.
    """

    def __init__(self, total: int, width: int = 30, stream=sys.stderr):
        self.total = total
        self.width = width
        self.stream = stream
        self.enabled = total > 0 and hasattr(stream, "isatty") and stream.isatty()
        self.n = 0
        self._last = 0.0

    def update(self, n: int, force: bool = False) -> None:
        self.n += n
        if not self.enabled:
            return
        now = time.monotonic()
        if not force and now - self._last < 0.1 and self.n < self.total:
            return
        self._last = now
        frac = min(self.n / self.total, 1.0) if self.total else 1.0
        filled = int(self.width * frac)
        bar = "█" * filled + "░" * (self.width - filled)
        mb = self.n / 1_000_000
        self.stream.write(f"\r\x1b[K  [{bar}] {frac * 100:5.1f}%  {mb:,.1f} MB")
        self.stream.flush()

    def log(self, msg: str) -> None:
        """Print msg above the bar, then redraw the bar."""
        if self.enabled:
            self.stream.write("\r\x1b[K")  # erase the current bar line
            self.stream.flush()
        print(msg)
        self.update(0, force=True)

    def close(self) -> None:
        if self.enabled:
            self.update(0, force=True)
            self.stream.write("\n")
            self.stream.flush()


def extract(archive: str, outdir: str, verbose: bool = False) -> int:
    """Stream every file out of a .wpress archive. Returns file count.

    Shows a progress bar on a terminal (always). With verbose=True it also lists
    each file as it's written; otherwise only the bar and a one-line summary
    appear.

    A real backup is mostly thousands of small uploads, so this path is
    per-file-overhead bound, not throughput bound. We hoist the traversal-guard
    base out of the loop and remember directories we've already created, so a
    1000-file directory costs one `makedirs`, not a thousand. (Benchmarked: the
    1 MiB-chunk copy loop is already at the SSD's bandwidth ceiling — the wins
    here are all in avoiding redundant per-file syscalls.)
    """
    base = os.path.abspath(outdir)
    base_prefix = base + os.sep
    made_dirs: set[str] = set()
    count = 0
    total_bytes = 0
    bar = _Progress(os.path.getsize(archive))
    with open(archive, "rb") as fh:
        while True:
            block = fh.read(HEADER_SIZE)
            bar.update(len(block))
            entry = _parse_header(block)
            if entry is None:
                break

            dest = os.path.abspath(os.path.join(base, entry.relpath))
            # Guard against path traversal from a crafted archive: dest must be
            # the outdir itself or strictly beneath it (the os.sep on base_prefix
            # stops a sibling like ".../outdir-evil" from sneaking through).
            if dest != base and not dest.startswith(base_prefix):
                raise ValueError(f"refusing to write outside outdir: {dest!r}")
            parent = os.path.dirname(dest)
            if parent not in made_dirs:
                os.makedirs(parent, exist_ok=True)
                made_dirs.add(parent)

            remaining = entry.size
            with open(dest, "wb") as out:
                while remaining > 0:
                    chunk = fh.read(min(READ_CHUNK, remaining))
                    if not chunk:
                        raise EOFError(
                            f"archive truncated while reading {entry.relpath!r} "
                            f"({remaining} bytes short)"
                        )
                    out.write(chunk)
                    remaining -= len(chunk)
                    bar.update(len(chunk))

            count += 1
            total_bytes += entry.size
            if verbose:
                bar.log(f"  {entry.size:>12,}  {entry.relpath}")

    bar.close()
    print(f"Extracted {count} files ({total_bytes:,} bytes) to {outdir}")
    return count


# ----------------------------------------------------------------------------
# PHP-serialization-safe string replacement
#
# A WordPress DB cell is either a plain string or a PHP-serialized blob.
# Serialized strings encode their *byte* length: s:5:"hello";. Replacing text
# inside without fixing that length corrupts the blob. We walk the structure
# using the declared lengths (never guessing terminators), replace inside string
# payloads, and re-emit with recomputed lengths. Strings whose payload is itself
# serialized (double-serialized data) are handled recursively.
# ----------------------------------------------------------------------------


class _ParseError(Exception):
    pass


def replace_serialized(value: bytes, old: bytes, new: bytes) -> bytes:
    """Replace old->new inside a value, preserving PHP serialization if present.

    Falls back to a plain byte replacement when the value isn't valid serialized
    data, so it's safe to call on any cell.
    """
    if old not in value:
        return value
    try:
        out = bytearray()
        end = _walk(value, 0, old, new, out)
        if end == len(value):  # consumed exactly -> it was serialized
            return bytes(out)
    except (_ParseError, ValueError, IndexError):
        pass
    return value.replace(old, new)


def _read_int(data: bytes, i: int, terminator: bytes) -> tuple[int, int]:
    j = data.index(terminator, i)
    return int(data[i:j]), j


def _walk(data: bytes, i: int, old: bytes, new: bytes, out: bytearray) -> int:
    """Transform one serialized token starting at i; append to out; return new i."""
    tok = data[i : i + 1]

    if tok == b"N":  # null: N;
        if data[i : i + 2] != b"N;":
            raise _ParseError("bad null")
        out += b"N;"
        return i + 2

    if tok in (b"b", b"i", b"d"):  # scalar: x:<...>;
        j = data.index(b";", i)
        out += data[i : j + 1]
        return j + 1

    if tok == b"s":  # string: s:<len>:"<bytes>";
        if data[i + 1 : i + 2] != b":":
            raise _ParseError("bad string header")
        length, colon = _read_int(data, i + 2, b":")
        if data[colon + 1 : colon + 2] != b'"':
            raise _ParseError("missing opening quote")
        start = colon + 2
        end = start + length
        if data[end : end + 2] != b'";':
            raise _ParseError("bad string terminator/length")
        raw = data[start:end]
        # Recurse: payload may itself be serialized (double-serialization).
        replaced = replace_serialized(raw, old, new)
        out += b"s:" + str(len(replaced)).encode() + b':"' + replaced + b'";'
        return end + 2

    if tok == b"a":  # array: a:<count>:{ k v k v ... }
        count, colon = _read_int(data, i + 2, b":")
        if data[colon + 1 : colon + 2] != b"{":
            raise _ParseError("missing array brace")
        out += data[i : colon + 2]
        j = colon + 2
        for _ in range(count * 2):  # each element = key + value
            j = _walk(data, j, old, new, out)
        if data[j : j + 1] != b"}":
            raise _ParseError("missing array close")
        out += b"}"
        return j + 1

    if tok == b"O":  # object: O:<len>:"class":<count>:{ name value ... }
        clslen, colon = _read_int(data, i + 2, b":")
        cstart = colon + 2  # past the opening quote
        cend = cstart + clslen
        if data[colon + 1 : colon + 2] != b'"' or data[cend : cend + 2] != b'":':
            raise _ParseError("bad object class")
        count, colon3 = _read_int(data, cend + 2, b":")
        if data[colon3 + 1 : colon3 + 2] != b"{":
            raise _ParseError("missing object brace")
        out += data[i : colon3 + 2]  # class name kept verbatim
        j = colon3 + 2
        for _ in range(count * 2):
            j = _walk(data, j, old, new, out)
        if data[j : j + 1] != b"}":
            raise _ParseError("missing object close")
        out += b"}"
        return j + 1

    raise _ParseError(f"unexpected token {tok!r} at offset {i}")


# ----------------------------------------------------------------------------
# Database operations
# ----------------------------------------------------------------------------


@dataclass
class DBConfig:
    database: str
    user: str
    password: str
    host: str = "localhost"
    port: int = 3306
    socket: str | None = None


def _mysql_base_args(cfg: DBConfig) -> list[str]:
    args = ["mysql", f"-u{cfg.user}", f"-p{cfg.password}", cfg.database]
    if cfg.socket:
        args.insert(1, f"--socket={cfg.socket}")
    else:
        args[1:1] = [f"-h{cfg.host}", f"-P{cfg.port}"]
    return args


def import_db(sql_path: str, cfg: DBConfig) -> None:
    """Stream a .sql dump into MySQL via the client binary."""
    if not os.path.exists(sql_path):
        raise FileNotFoundError(sql_path)
    size = os.path.getsize(sql_path)
    print(f"Importing {sql_path} ({size:,} bytes) into `{cfg.database}` ...")
    with open(sql_path, "rb") as fh:
        proc = subprocess.run(_mysql_base_args(cfg), stdin=fh)
    if proc.returncode != 0:
        raise RuntimeError(f"mysql import failed (exit {proc.returncode})")
    print("Import complete.")


def _connect(cfg: DBConfig):
    import pymysql

    return pymysql.connect(
        host=cfg.host,
        port=cfg.port,
        user=cfg.user,
        password=cfg.password,
        database=cfg.database,
        unix_socket=cfg.socket,
        charset="utf8mb4",
        use_unicode=False,  # work in raw bytes so lengths match PHP's view
    )


def _string_columns(cursor, database: str):
    """Yield (table, column, pk_columns) for every text/blob/char column."""
    cursor.execute(
        """
        SELECT TABLE_NAME, COLUMN_NAME
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = %s
          AND DATA_TYPE IN ('char','varchar','tinytext','text','mediumtext',
                            'longtext','tinyblob','blob','mediumblob','longblob')
        ORDER BY TABLE_NAME, ORDINAL_POSITION
        """,
        (database,),
    )
    cols: dict[bytes, list[bytes]] = {}
    for table, column in cursor.fetchall():
        cols.setdefault(table, []).append(column)

    pks = _primary_keys(cursor, database)
    for table, columns in cols.items():
        yield table, columns, pks.get(table, [])


def _primary_keys(cursor, database: str) -> dict[bytes, list[bytes]]:
    cursor.execute(
        """
        SELECT TABLE_NAME, COLUMN_NAME
        FROM information_schema.KEY_COLUMN_USAGE
        WHERE TABLE_SCHEMA = %s AND CONSTRAINT_NAME = 'PRIMARY'
        ORDER BY TABLE_NAME, ORDINAL_POSITION
        """,
        (database,),
    )
    pks: dict[bytes, list[bytes]] = {}
    for table, column in cursor.fetchall():
        pks.setdefault(table, []).append(column)
    return pks


def _ident(name: bytes) -> str:
    """Quote a MySQL identifier (bytes from information_schema)."""
    s = name.decode("utf-8")
    return "`" + s.replace("`", "``") + "`"


def search_replace(cfg: DBConfig, old: bytes, new: bytes, dry_run: bool = False) -> int:
    """Serialization-safe replace of old->new across all text columns.

    Returns the number of cells changed.
    """
    conn = _connect(cfg)
    changed_cells = 0
    changed_tables: dict[str, int] = {}
    try:
        with conn.cursor() as meta:
            targets = list(_string_columns(meta, cfg.database))

        for table, columns, pk in targets:
            if not pk:
                print(f"  ! skipping {table.decode()} (no primary key)", file=sys.stderr)
                continue

            tname = _ident(table)
            pk_idents = [_ident(c) for c in pk]
            select_cols = ", ".join(pk_idents + [_ident(c) for c in columns])

            with conn.cursor() as read, conn.cursor() as write:
                read.execute(f"SELECT {select_cols} FROM {tname}")
                while True:
                    rows = read.fetchmany(500)
                    if not rows:
                        break
                    for row in rows:
                        key_vals = row[: len(pk)]
                        col_vals = row[len(pk) :]
                        updates = {}
                        for col, val in zip(columns, col_vals):
                            if val is None or old not in val:
                                continue
                            new_val = replace_serialized(val, old, new)
                            if new_val != val:
                                updates[col] = new_val
                        if not updates:
                            continue
                        changed_cells += len(updates)
                        changed_tables[table.decode()] = (
                            changed_tables.get(table.decode(), 0) + len(updates)
                        )
                        if dry_run:
                            continue
                        set_clause = ", ".join(f"{_ident(c)} = %s" for c in updates)
                        where_clause = " AND ".join(f"{c} = %s" for c in pk_idents)
                        write.execute(
                            f"UPDATE {tname} SET {set_clause} WHERE {where_clause}",
                            tuple(updates.values()) + tuple(key_vals),
                        )
        if not dry_run:
            conn.commit()
    finally:
        conn.close()

    verb = "Would change" if dry_run else "Changed"
    print(f"\n{verb} {changed_cells} cell(s) across {len(changed_tables)} table(s):")
    for t, n in sorted(changed_tables.items()):
        print(f"  {n:>6}  {t}")
    return changed_cells


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------


def _add_db_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--db", required=True, help="database name")
    p.add_argument("--user", required=True)
    p.add_argument("--password", required=True)
    p.add_argument("--host", default="localhost")
    p.add_argument("--port", type=int, default=3306)
    p.add_argument("--socket", default=None, help="unix socket path (overrides host/port)")


def _cfg(a) -> DBConfig:
    return DBConfig(a.db, a.user, a.password, a.host, a.port, a.socket)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="wpress-migrate", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    pe = sub.add_parser("extract", help="unpack a .wpress archive")
    pe.add_argument("archive")
    pe.add_argument("-o", "--outdir", default="wpress-extracted")
    pe.add_argument("-v", "--verbose", action="store_true",
                    help="list every file as it's extracted (a progress bar shows either way)")

    pi = sub.add_parser("import-db", help="stream database.sql into MySQL")
    pi.add_argument("sql")
    _add_db_args(pi)

    ps = sub.add_parser("search-replace", help="serialization-safe string replace")
    ps.add_argument("old")
    ps.add_argument("new")
    ps.add_argument("--dry-run", action="store_true")
    _add_db_args(ps)

    pm = sub.add_parser("migrate", help="extract + import-db + search-replace")
    pm.add_argument("archive")
    pm.add_argument("-o", "--outdir", default="wpress-extracted")
    pm.add_argument("--old-url", required=True)
    pm.add_argument("--new-url", required=True)
    pm.add_argument("--dry-run", action="store_true")
    _add_db_args(pm)

    a = parser.parse_args(argv)

    if a.cmd == "extract":
        extract(a.archive, a.outdir, a.verbose)
        return 0

    if a.cmd == "import-db":
        import_db(a.sql, _cfg(a))
        return 0

    if a.cmd == "search-replace":
        search_replace(_cfg(a), a.old.encode(), a.new.encode(), a.dry_run)
        return 0

    if a.cmd == "migrate":
        print("== 1/3 extract ==")
        extract(a.archive, a.outdir, verbose=False)
        sql = os.path.join(a.outdir, "database.sql")
        print(f"   files in {a.outdir}; database at {sql}")
        print("== 2/3 import database ==")
        import_db(sql, _cfg(a))
        print("== 3/3 search-replace ==")
        search_replace(_cfg(a), a.old_url.encode(), a.new_url.encode(), a.dry_run)
        print(
            "\nNext steps (manual):\n"
            f"  1. Copy {a.outdir}/uploads, /themes, /plugins into your WP wp-content/\n"
            "  2. Set $table_prefix in wp-config.php to match the imported tables\n"
            "  3. Flush permalinks: `wp rewrite flush --hard` (or Settings > Permalinks > Save)\n"
        )
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
