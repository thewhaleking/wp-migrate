import io, os, struct, tempfile, shutil
import phpserialize as php
import wpress_migrate as wm

PASS = 0; FAIL = 0
def check(name, cond):
    global PASS, FAIL
    if cond: PASS += 1; print(f"  PASS  {name}")
    else: FAIL += 1; print(f"  FAIL  {name}")

# ---------------------------------------------------------------------------
# 1. Archive round-trip: build a synthetic .wpress, extract, compare bytes.
# ---------------------------------------------------------------------------
def make_header(name: str, content: bytes, prefix: str) -> bytes:
    b = bytearray(b"\x00" * wm.HEADER_SIZE)
    b[0:len(name.encode())] = name.encode()
    sz = str(len(content)).encode(); b[255:255+len(sz)] = sz
    mt = b"1700000000"; b[269:269+len(mt)] = mt
    p = prefix.encode(); b[281:281+len(p)] = p
    return bytes(b)

files = {
    ("database.sql", "."): b"-- dump\nINSERT INTO wp_options VALUES (1,'siteurl','http://old');\n",
    ("logo.png", "uploads/2026/01"): os.urandom(2_500_000),     # binary, multi-chunk
    ("style.css", "themes/x"): b"body{color:red}" * 1000,
    ("empty.txt", "uploads"): b"",                               # zero-length edge
}
arch = io.BytesIO()
for (name, prefix), content in files.items():
    arch.write(make_header(name, content, prefix)); arch.write(content)
arch.write(wm.EOF_BLOCK)

tmp = tempfile.mkdtemp()
ap = os.path.join(tmp, "test.wpress")
with open(ap, "wb") as f: f.write(arch.getvalue())
out = os.path.join(tmp, "out")
n = wm.extract(ap, out, verbose=False)
check("extract returns correct file count", n == len(files))
for (name, prefix), content in files.items():
    rel = name if prefix == "." else os.path.join(prefix, name)
    p = os.path.join(out, rel)
    ok = os.path.exists(p) and open(p, "rb").read() == content
    check(f"roundtrip bytes match: {rel}", ok)

# verbose=True must take the per-file log path without error (bar self-disables
# off a TTY) and still extract everything.
out_v = os.path.join(tmp, "out_v")
check("verbose extract returns correct count", wm.extract(ap, out_v, verbose=True) == len(files))
shutil.rmtree(tmp)

# ---------------------------------------------------------------------------
# 1b. Path-traversal guard: a crafted prefix must be refused, not written.
# ---------------------------------------------------------------------------
tmp = tempfile.mkdtemp()
evil = io.BytesIO()
evil.write(make_header("evil.txt", b"pwned", "../../../../tmp")); evil.write(b"pwned")
evil.write(wm.EOF_BLOCK)
ap = os.path.join(tmp, "evil.wpress")
with open(ap, "wb") as f: f.write(evil.getvalue())
out = os.path.join(tmp, "out")
try:
    wm.extract(ap, out, verbose=False)
    check("path traversal rejected", False)
except ValueError:
    check("path traversal rejected", True)
shutil.rmtree(tmp)

# ---------------------------------------------------------------------------
# 1c. Many files in one directory: dir-cache must not break creation.
# ---------------------------------------------------------------------------
tmp = tempfile.mkdtemp()
many = io.BytesIO()
expected_files = {}
for k in range(250):
    name, prefix, content = f"img{k:04d}.bin", "uploads/2026/06", bytes([k % 256]) * (k + 1)
    many.write(make_header(name, content, prefix)); many.write(content)
    expected_files[os.path.join(prefix, name)] = content
many.write(wm.EOF_BLOCK)
ap = os.path.join(tmp, "many.wpress")
with open(ap, "wb") as f: f.write(many.getvalue())
out = os.path.join(tmp, "out")
n = wm.extract(ap, out, verbose=False)
ok = n == len(expected_files) and all(
    os.path.exists(os.path.join(out, rel)) and open(os.path.join(out, rel), "rb").read() == c
    for rel, c in expected_files.items()
)
check("many files in one dir extract correctly", ok)
shutil.rmtree(tmp)

# ---------------------------------------------------------------------------
# 2. Serialization-safe replace, validated against phpserialize as oracle.
# ---------------------------------------------------------------------------
OLD = b"http://old.example.com"
NEW = b"https://brand-new-domain.io"          # different length on purpose

def deep_replace(obj):
    if isinstance(obj, bytes): return obj.replace(OLD, NEW)
    if isinstance(obj, dict):
        return {deep_replace(k): deep_replace(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [deep_replace(v) for v in obj]
    return obj

cases = {
    "plain string": b"go to http://old.example.com today",
    "string with embedded quote-semicolon": b'a quote"; then http://old.example.com',
    "flat array": {b"siteurl": b"http://old.example.com", b"home": b"http://old.example.com/blog"},
    "nested array + scalars": {
        b"opt": {b"url": b"http://old.example.com", b"count": 42, b"ratio": 1.5,
                 b"on": True, b"nada": None,
                 b"list": [b"http://old.example.com/a", b"http://old.example.com/b"]},
    },
    "key also contains url": {b"http://old.example.com/key": b"val http://old.example.com"},
    "no match (unchanged)": {b"x": b"nothing here"},
}
for name, obj in cases.items():
    ser = php.dumps(obj)
    out = wm.replace_serialized(ser, OLD, NEW)
    # The transformed bytes must (a) still be valid PHP serialization and
    # (b) unserialize to the deep-replaced structure.
    try:
        reparsed = php.loads(out, decode_strings=False)
    except Exception as e:
        check(f"serialized [{name}] re-parses", False); continue
    expected = deep_replace(obj)
    expected_norm = php.loads(php.dumps(expected), decode_strings=False)
    check(f"serialized [{name}] correct", reparsed == expected_norm)

# Double-serialized: a string value that is itself a serialized blob.
inner = php.dumps({b"url": b"http://old.example.com"})
outer = php.dumps({b"widget_data": inner})
out = wm.replace_serialized(outer, OLD, NEW)
reparsed = php.loads(out, decode_strings=False)
inner_fixed = reparsed[b"widget_data"]
inner_expected = php.dumps({b"url": NEW})
check("double-serialized inner length fixed", inner_fixed == inner_expected)

# Object (O:) token
obj_ser = b'O:8:"stdClass":1:{s:3:"url";s:22:"http://old.example.com";}'
out = wm.replace_serialized(obj_ser, OLD, NEW)
check("object token: length recomputed",
      out == b'O:8:"stdClass":1:{s:3:"url";s:27:"https://brand-new-domain.io";}')

# Plain (non-serialized) text falls back to plain replace
plain = b"just text http://old.example.com end"
check("non-serialized falls back to plain replace",
      wm.replace_serialized(plain, OLD, NEW) == plain.replace(OLD, NEW))

# ---------------------------------------------------------------------------
# 3. fix_empty_hex: AIO's `0x` empty-blob literal -> '' (string-aware).
# ---------------------------------------------------------------------------
def fx(line):
    return wm.fix_empty_hex(line)

# the exact failing statement from the field report
real = b"INSERT INTO `SERVMASK_PREFIX_wfConfig` VALUES ('lastScanFailureType',0x,'yes')\n"
check("empty hex 0x replaced with ''",
      fx(real) == (b"INSERT INTO `SERVMASK_PREFIX_wfConfig` VALUES ('lastScanFailureType','',"
                   b"'yes')\n", 1))
check("real hex literal left intact", fx(b"VALUES (1,0x4142,'x')\n") == (b"VALUES (1,0x4142,'x')\n", 0))
check("0x inside a string is untouched", fx(b"VALUES ('foo 0x bar')\n") == (b"VALUES ('foo 0x bar')\n", 0))
check("0x with delimiters inside a string is untouched",
      fx(b"VALUES ('a,0x,b')\n") == (b"VALUES ('a,0x,b')\n", 0))
check("0x inside a backtick identifier is untouched",
      fx(b"INSERT INTO `t0x` VALUES (0x)\n") == (b"INSERT INTO `t0x` VALUES ('')\n", 1))
check("multiple empty-hex on one line", fx(b"VALUES (0x,0x,'k')\n") == (b"VALUES ('','','k')\n", 2))
check("doubled-quote string then empty hex",
      fx(b"VALUES ('it''s',0x)\n") == (b"VALUES ('it''s','')\n", 1))
check("backslash-escaped quote then string-internal 0x untouched, outer fixed",
      fx(b"VALUES ('a\\' 0x z',0x)\n") == (b"VALUES ('a\\' 0x z','')\n", 1))
check("line without 0x is a no-op", fx(b"VALUES (1,2,3)\n") == (b"VALUES (1,2,3)\n", 0))

# ---------------------------------------------------------------------------
# 4. --table-prefix: rewrite SERVMASK_PREFIX_ in identifiers (stream) and in
#    row data (serialization-safe, second pass).
# ---------------------------------------------------------------------------
def px(line, src=b"SERVMASK_PREFIX_", dst=b"wp_"):
    return wm._scan_sql_line(line, src, dst)

# Backtick identifier renamed; the SAME placeholder inside quoted data is left
# for the serialization-safe pass; bare 0x still repaired. (returns (line,hex,pref))
ins = b"INSERT INTO `SERVMASK_PREFIX_usermeta` VALUES (1,5,'SERVMASK_PREFIX_capabilities',0x)\n"
check("prefix: identifier renamed, quoted data untouched, hex fixed",
      px(ins) == (b"INSERT INTO `wp_usermeta` VALUES (1,5,'SERVMASK_PREFIX_capabilities','')\n", 1, 1))
check("prefix: DROP identifier renamed",
      px(b"DROP TABLE IF EXISTS `SERVMASK_PREFIX_posts`;\n")
      == (b"DROP TABLE IF EXISTS `wp_posts`;\n", 0, 1))
check("prefix: no rewrite when src==dst",
      wm._scan_sql_line(b"INSERT INTO `wp_x` VALUES (1)\n", b"wp_", b"wp_") == (b"INSERT INTO `wp_x` VALUES (1)\n", 0, 0))
check("fix_empty_hex wrapper still returns 2-tuple",
      wm.fix_empty_hex(b"VALUES (0x)\n") == (b"VALUES ('')\n", 1))

# Data-side: the serialization-safe engine fixes embedded prefix refs, including
# inside serialized blobs where the byte length must be recomputed.
check("prefix in plain value -> plain replace",
      wm.replace_serialized(b"SERVMASK_PREFIX_capabilities", b"SERVMASK_PREFIX_", b"wp_") == b"wp_capabilities")
ser = php.dumps({b"k": b"SERVMASK_PREFIX_options"})
fixed = wm.replace_serialized(ser, b"SERVMASK_PREFIX_", b"wp_")
check("prefix inside serialized value -> length recomputed",
      php.loads(fixed, decode_strings=False) == {b"k": b"wp_options"})

print(f"\n{PASS} passed, {FAIL} failed")
raise SystemExit(1 if FAIL else 0)
