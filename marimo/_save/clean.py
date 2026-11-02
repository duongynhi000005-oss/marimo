# Copyright 2026 Marimo. All rights reserved.
"""Deletion of the cache entries a notebook's manifest lists.

A manifest records the entries a notebook produced. Cleaning a notebook
deletes exactly those, then forgets the records that no file on disk
answers to any more, so the manifest keeps describing what the cache
holds. Entries the manifest does not list are left in place.

Pure and kernel-free so the CLI can import it without starting a runtime.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from marimo._save.cache_dirs import (
    CacheDirStats,
    block_dir_name,
    delete_cache_entries,
    holds_entry,
)
from marimo._save.manifest import (
    CacheManifest,
    load_manifest,
    manifest_name,
)

if TYPE_CHECKING:
    from collections.abc import Collection
    from pathlib import Path


@dataclass(frozen=True)
class CleanPlan:
    """The planned clean of one notebook's entries in one cache directory."""

    cache_dir: Path
    # The store key of the notebook's manifest in `cache_dir`.
    key: str
    # `None` when the directory holds no manifest for the notebook.
    manifest: CacheManifest | None = None
    # The entries to delete, and what deleting them is expected to free.
    entries: frozenset[tuple[str, str]] = frozenset()
    freed: CacheDirStats = field(default_factory=CacheDirStats)

    def deletes_nothing(self) -> bool:
        """Whether no file on disk answers to any planned entry.

        Such records hold nothing, so forgetting them takes nothing away
        from the cache.
        """
        return not self.freed.entries and not self.freed.total_bytes


def plan_clean(
    cache_dir: Path, notebook: Path, names: Collection[str] = ()
) -> CleanPlan:
    """Plan the clean of `notebook`'s entries in `cache_dir`, deleting nothing.

    `names` are the names given to `mo.persistent_cache`; an empty `names`
    plans every entry the manifest lists.

    Raises:
        ManifestError: If the manifest cannot be read as one.
        OSError: If the manifest cannot be read at all.
    """
    from marimo._save.stores.file import FileStore

    key = manifest_name(notebook)
    manifest = None
    if cache_dir.is_dir():
        manifest = load_manifest(FileStore(save_path=str(cache_dir)), key)
    if manifest is None:
        return CleanPlan(cache_dir, key)

    entries = manifest.entries()
    if names:
        wanted = {block_dir_name(name) for name in names}
        entries = {entry for entry in entries if entry[0] in wanted}
    return CleanPlan(
        cache_dir,
        key,
        manifest,
        frozenset(entries),
        delete_cache_entries(cache_dir, entries, dry_run=True),
    )


def apply_clean(plan: CleanPlan) -> CacheDirStats:
    """Carry out `plan`, reporting what it freed.

    A record whose entry could not be removed is kept. The entry is still
    there to be read, and to be deleted another time. The manifest is
    rewritten after the entries are gone, so a run that dies midway leaves
    records of entries the cache no longer holds rather than entries
    nothing records.

    Raises:
        OSError: If the manifest cannot be rewritten.
    """
    from marimo._save.stores.file import FileStore

    if plan.manifest is None:
        return CacheDirStats()
    freed = delete_cache_entries(plan.cache_dir, plan.entries)
    plan.manifest.discard(
        {
            entry
            for entry in plan.entries
            if not holds_entry(plan.cache_dir, *entry)
        }
    )
    FileStore(save_path=str(plan.cache_dir)).put(
        plan.key, plan.manifest.to_bytes()
    )
    return freed
