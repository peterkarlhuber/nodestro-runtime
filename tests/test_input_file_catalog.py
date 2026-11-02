"""The volume's catalog of input files keeps every update of every process (security review SR-08).

On a server the API, the workers and commands such as ``files remove-source`` share one volume. Each of them
reads ``catalog.json``, changes it and writes it back; without a lock across processes one of two concurrent
updates was lost (a deleted entry came back). Every save and removal now holds ``catalog.lock``.
"""

from __future__ import annotations

import fcntl
import json
import multiprocessing
import os
from pathlib import Path
import threading

from nodestro_runtime.input_files import CATALOG_LOCK, _Blobs


def _catalog(root: Path) -> set[str]:
    return set(json.loads((root / "catalog.json").read_text(encoding="utf-8")))


def test_a_removal_waits_for_another_process_that_holds_the_catalog(tmp_path: Path) -> None:
    root = tmp_path / "inputs"
    blobs = _Blobs(root)
    first = blobs.put(b"first", name="a.txt", media_type="text/plain")
    second = blobs.put(b"second", name="b.txt", media_type="text/plain")
    # Another process (here: another open file) is in the middle of its read-modify-write of the catalog.
    other = os.open(root / CATALOG_LOCK, os.O_RDWR)
    fcntl.flock(other, fcntl.LOCK_EX)
    stale = json.loads((root / "catalog.json").read_text(encoding="utf-8"))
    removal = threading.Thread(target=_Blobs(root).delete, args=(first,))
    removal.start()
    try:
        removal.join(0.5)
        assert removal.is_alive(), "the removal did not wait for the catalog lock"
        stale.pop(second)  # the other process removes the second entry from what it read
        (root / "catalog.json").write_text(json.dumps(stale), encoding="utf-8")
    finally:
        os.close(other)
    removal.join(10)
    assert not removal.is_alive()
    assert _catalog(root) == set()  # both removals stay; neither entry comes back


def _put_many(root: str, worker: int, count: int) -> None:
    blobs = _Blobs(root)
    for number in range(count):
        blobs.put(f"worker {worker} file {number}".encode(), name="x.txt", media_type="text/plain")


def test_concurrent_processes_lose_no_catalog_entry(tmp_path: Path) -> None:
    root = tmp_path / "inputs"
    keep = _Blobs(root)
    doomed = [keep.put(f"doomed {number}".encode(), name="d.txt", media_type="text/plain") for number in range(20)]
    context = multiprocessing.get_context("fork")
    workers = [context.Process(target=_put_many, args=(str(root), worker, 30)) for worker in range(4)]
    for process in workers:
        process.start()
    for blob in doomed:  # a remove-source running at the same time
        keep.delete(blob)
    for process in workers:
        process.join(60)
        assert process.exitcode == 0
    entries = _catalog(root)
    assert len(entries) == 4 * 30
    assert not entries & set(doomed)
