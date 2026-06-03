#!/usr/bin/env python3
"""Benchmark extract throughput across implementation variants.

Generates a synthetic .wpress archive (~GB scale), then times several
extraction strategies. The archive is warm in the page cache after
generation (48 GB RAM), so this measures the *code's* ceiling — interpreter
+ syscall overhead — not cold disk-read latency. If warm throughput already
exceeds real disk bandwidth, the code is not the bottleneck.
"""
from __future__ import annotations

import os
import sys
import time
import shutil
import tempfile
import threading
import queue
from concurrent.futures import ThreadPoolExecutor

import wpress_migrate as wm

HEADER_SIZE = wm.HEADER_SIZE


def make_header(name: str, size: int, prefix: str) -> bytes:
    b = bytearray(b"\x00" * HEADER_SIZE)
    nb = name.encode()
    b[0:len(nb)] = nb
    sz = str(size).encode(); b[255:255 + len(sz)] = sz
    mt = b"1700000000"; b[269:269 + len(mt)] = mt
    p = prefix.encode(); b[281:281 + len(p)] = p
    return bytes(b)


def build_archive(path: str, plan: list[tuple[str, str, int]]) -> int:
    """plan = [(name, prefix, size), ...]; returns total content bytes."""
    seed = os.urandom(16 * 1024 * 1024)  # 16 MiB reused block (gen is not what we measure)
    total = 0
    with open(path, "wb") as out:
        for name, prefix, size in plan:
            out.write(make_header(name, size, prefix))
            remaining = size
            while remaining > 0:
                chunk = seed[:min(len(seed), remaining)]
                out.write(chunk)
                remaining -= len(chunk)
            total += size
        out.write(wm.EOF_BLOCK)
    return total


# --- index pass: scan headers only, seeking past content -------------------
def index_archive(archive: str):
    """Return [(content_offset, size, relpath), ...] without reading content."""
    entries = []
    with open(archive, "rb") as fh:
        while True:
            block = fh.read(HEADER_SIZE)
            entry = wm._parse_header(block)
            if entry is None:
                break
            off = fh.tell()
            entries.append((off, entry.size, entry.relpath))
            fh.seek(entry.size, os.SEEK_CUR)
    return entries


# --- variant 1: readinto, reusable 1 MiB buffer ----------------------------
def extract_readinto(archive: str, outdir: str, bufsize: int) -> int:
    buf = bytearray(bufsize)
    view = memoryview(buf)
    count = 0
    with open(archive, "rb") as fh:
        while True:
            block = fh.read(HEADER_SIZE)
            entry = wm._parse_header(block)
            if entry is None:
                break
            dest = os.path.normpath(os.path.join(outdir, entry.relpath))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            remaining = entry.size
            with open(dest, "wb") as out:
                while remaining > 0:
                    n = fh.readinto(view[:min(bufsize, remaining)])
                    if not n:
                        raise EOFError("truncated")
                    out.write(view[:n])
                    remaining -= n
            count += 1
    return count


# --- variant 2: two-pass, parallel file writes via pread -------------------
def extract_parallel(archive: str, outdir: str, workers: int, bufsize: int) -> int:
    entries = index_archive(archive)
    # pre-create dirs single-threaded to avoid makedirs races
    for _, _, rel in entries:
        d = os.path.dirname(os.path.normpath(os.path.join(outdir, rel)))
        os.makedirs(d, exist_ok=True)

    def one(args):
        off, size, rel = args
        dest = os.path.normpath(os.path.join(outdir, rel))
        fd = os.open(archive, os.O_RDONLY)
        try:
            pos = off
            remaining = size
            with open(dest, "wb") as out:
                while remaining > 0:
                    chunk = os.pread(fd, min(bufsize, remaining), pos)
                    if not chunk:
                        raise EOFError("truncated")
                    out.write(chunk)
                    pos += len(chunk)
                    remaining -= len(chunk)
        finally:
            os.close(fd)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(one, entries))
    return len(entries)


# --- variant 3: producer/consumer (one reader thread, one writer thread) ---
def extract_pipelined(archive: str, outdir: str, bufsize: int, depth: int = 8) -> int:
    entries = index_archive(archive)
    for _, _, rel in entries:
        d = os.path.dirname(os.path.normpath(os.path.join(outdir, rel)))
        os.makedirs(d, exist_ok=True)
    q: queue.Queue = queue.Queue(maxsize=depth)
    STOP = object()

    def writer():
        out = None
        while True:
            item = q.get()
            if item is STOP:
                if out: out.close()
                return
            kind, payload = item
            if kind == "open":
                if out: out.close()
                out = open(payload, "wb")
            else:
                out.write(payload)

    t = threading.Thread(target=writer); t.start()
    with open(archive, "rb") as fh:
        for off, size, rel in entries:
            dest = os.path.normpath(os.path.join(outdir, rel))
            q.put(("open", dest))
            fh.seek(off)
            remaining = size
            while remaining > 0:
                chunk = fh.read(min(bufsize, remaining))
                if not chunk:
                    raise EOFError("truncated")
                q.put(("data", chunk))
                remaining -= len(chunk)
    q.put(STOP); t.join()
    return len(entries)


# --- candidate2: dir-cache to kill redundant makedirs ----------------------
def extract_dircache(archive: str, outdir: str, bufsize: int = 1 << 20) -> int:
    base = os.path.abspath(outdir)
    base_prefix = base + os.sep
    seen_dirs: set[str] = set()
    buf = bytearray(bufsize)
    view = memoryview(buf)
    count = 0
    with open(archive, "rb") as fh:
        while True:
            block = fh.read(HEADER_SIZE)
            entry = wm._parse_header(block)
            if entry is None:
                break
            dest = os.path.abspath(os.path.join(base, entry.relpath))
            if dest != base and not dest.startswith(base_prefix):
                raise ValueError(f"refusing to write outside outdir: {dest!r}")
            d = os.path.dirname(dest)
            if d not in seen_dirs:
                os.makedirs(d, exist_ok=True)
                seen_dirs.add(d)
            remaining = entry.size
            with open(dest, "wb") as out:
                while remaining > 0:
                    n = fh.readinto(view[:min(bufsize, remaining)])
                    if not n:
                        raise EOFError("truncated")
                    out.write(view[:n])
                    remaining -= n
            count += 1
    return count


# --- parallel + dir-cache (dirs pre-created single-threaded) ----------------
def extract_parallel_dc(archive: str, outdir: str, workers: int, bufsize: int = 1 << 20) -> int:
    base = os.path.abspath(outdir)
    base_prefix = base + os.sep
    entries = index_archive(archive)
    seen: set[str] = set()
    checked = []
    for off, size, rel in entries:
        dest = os.path.abspath(os.path.join(base, rel))
        if dest != base and not dest.startswith(base_prefix):
            raise ValueError(f"refusing to write outside outdir: {dest!r}")
        d = os.path.dirname(dest)
        if d not in seen:
            os.makedirs(d, exist_ok=True); seen.add(d)
        checked.append((off, size, dest))

    def one(args):
        off, size, dest = args
        fd = os.open(archive, os.O_RDONLY)
        try:
            pos, remaining = off, size
            with open(dest, "wb") as out:
                while remaining > 0:
                    chunk = os.pread(fd, min(bufsize, remaining), pos)
                    if not chunk:
                        raise EOFError("truncated")
                    out.write(chunk); pos += len(chunk); remaining -= len(chunk)
        finally:
            os.close(fd)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(one, checked, chunksize=64))
    return len(checked)


# --- production candidate: readinto + hoisted, cheap traversal guard -------
def extract_candidate(archive: str, outdir: str, bufsize: int = 1 << 20) -> int:
    base = os.path.abspath(outdir)
    base_prefix = base + os.sep
    buf = bytearray(bufsize)
    view = memoryview(buf)
    count = 0
    with open(archive, "rb") as fh:
        while True:
            block = fh.read(HEADER_SIZE)
            entry = wm._parse_header(block)
            if entry is None:
                break
            dest = os.path.abspath(os.path.join(base, entry.relpath))
            if dest != base and not dest.startswith(base_prefix):
                raise ValueError(f"refusing to write outside outdir: {dest!r}")
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            remaining = entry.size
            with open(dest, "wb") as out:
                while remaining > 0:
                    n = fh.readinto(view[:min(bufsize, remaining)])
                    if not n:
                        raise EOFError("truncated")
                    out.write(view[:n])
                    remaining -= n
            count += 1
    return count


def timed(label, fn, outdir, total_bytes):
    if os.path.exists(outdir):
        shutil.rmtree(outdir)
    os.makedirs(outdir)
    t0 = time.perf_counter()
    n = fn(outdir)
    # fsync-free; OS writeback. Measure wall time of the API as callers see it.
    dt = time.perf_counter() - t0
    gbps = total_bytes / dt / 1e9
    print(f"  {label:<34} {dt:7.3f}s   {gbps*1000:7.1f} MB/s   ({n} files)")
    return dt


def main():
    gb = float(sys.argv[1]) if len(sys.argv) > 1 else 3.0
    tmp = tempfile.mkdtemp(prefix="bench_wpress_")
    archive = os.path.join(tmp, "test.wpress")
    print(f"Building ~{gb:.1f} GB archive in {tmp} ...")

    # mode: "mixed" (few big + medium) or "tiny" (many small files = per-file overhead)
    mode = sys.argv[2] if len(sys.argv) > 2 else "mixed"
    plan = []
    target = int(gb * 1e9)
    acc = 0
    i = 0
    if mode == "tiny":
        size_fixed = 10 * 1024            # 10 KiB each -> ~100k files per GB
        while acc < target:
            plan.append((f"f{i:06d}.jpg", f"uploads/{i // 1000:03d}", size_fixed))
            acc += size_fixed
            i += 1
    else:
        while acc < target:
            size = 40 * 1024 * 1024 if i % 50 == 0 else 300 * 1024
            plan.append((f"f{i:06d}.bin", f"uploads/{i // 1000:03d}", size))
            acc += size
            i += 1
    total = build_archive(archive, plan)
    print(f"  {len(plan)} files, {total/1e9:.2f} GB content, archive on disk + warm in cache\n")

    out = os.path.join(tmp, "out")
    print("Warming up (sync + 1 discarded extract to remove cold-start artifact) ...")
    os.system("sync")
    extract_candidate(archive, out + "_warm"); shutil.rmtree(out + "_warm")

    print("\nThroughput (steady-state, candidate first to disprove order bias):")
    for _ in range(2):
        timed("v0 current", lambda o: wm.extract(archive, o, verbose=False), out, total)
        timed("v6 dir-cache (1 thread)", lambda o: extract_dircache(archive, o), out, total)
    for w in (4, 8, 14, 28):
        timed(f"v7 parallel+dircache x{w}", lambda o, w=w: extract_parallel_dc(archive, o, w), out, total)

    print(f"\nRaw kernel copy baseline (cp of the archive):")
    cpdst = os.path.join(tmp, "copy.wpress")
    t0 = time.perf_counter()
    os.system(f"cp {archive!r} {cpdst!r}")
    dt = time.perf_counter() - t0
    print(f"  {'cp archive->archive':<34} {dt:7.3f}s   {total/dt/1e9*1000:7.1f} MB/s")

    shutil.rmtree(tmp)


if __name__ == "__main__":
    main()