"""Recovery of derived month values, independent of all accounting writers."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, replace
import re

from .monthly_projection import (Category, CategoryCatalog, IndexEntry, LedgerIndex, MonthProjection,
                                 ProjectionError, Purchase, month_key, rebuild_month)
from .projection_store import ProjectionJournal, empty_journal, merge_ranges, replace_document


def catalog_document(catalog):
    return {"categories": [{**asdict(c), "aliases": [list(a) for a in c.aliases]} for c in catalog.categories]}


def load_catalog(value):
    try:
        return CategoryCatalog(Category(**{**c, "aliases": tuple(tuple(a) for a in c["aliases"])})
                               for c in value["categories"])
    except (KeyError, TypeError, ValueError):
        raise ProjectionError("projection_catalog_invalid") from None


def index_document(index):
    return {"rows": [[key, row, index.by_id[key].month if key in index.by_id else "",
                      index.by_id[key].fingerprint if key in index.by_id else ""]
                     for row, key in sorted(index.by_row.items())]}


def load_index(value):
    index = LedgerIndex()
    try:
        for key, row, month, digest in value["rows"]:
            if (not isinstance(key, str) or not key or type(row) is not int or row < 2
                    or key in index.identity_rows or row in index.by_row):
                raise ValueError
            index.by_row[row], index.identity_rows[key] = key, row
            if month:
                month_key(month)
                if not re.fullmatch(r"[a-f0-9]{64}", digest):
                    raise ValueError
                index.by_id[key] = IndexEntry(row, month, digest)
                index.by_month[month].add(key)
            elif digest:
                raise ValueError
        return index
    except (KeyError, TypeError, ValueError):
        raise ProjectionError("projection_index_invalid") from None


def month_document(projection):
    return {**asdict(projection), "category_amounts": [list(x) for x in projection.category_amounts],
            "purchases": [{**asdict(p), "categories": list(p.categories), "expense_ids": list(p.expense_ids)}
                          for p in projection.purchases]}


def load_month(value):
    if value is None:
        return None
    try:
        return MonthProjection(**{**value, "category_amounts": tuple(tuple(x) for x in value["category_amounts"]),
            "purchases": tuple(Purchase(**{**p, "categories": tuple(p["categories"]),
                "expense_ids": tuple(p.get("expense_ids",()))}) for p in value["purchases"])})
    except (KeyError, TypeError, ValueError):
        raise ProjectionError("projection_month_invalid") from None


def totals(projection):
    return {k: v for k, v in month_document(projection).items() if k != "purchases"}


def bounded_ranges(ranges, limit):
    for first, last in merge_ranges(ranges):
        for start in range(first, last + 1, limit):
            yield start, min(last, start + limit - 1)


class ProjectionRefresh:
    def __init__(self, store, reader):
        self.store, self.reader = store, reader
        self.journal = ProjectionJournal(store)

    def _save(self, key, value):
        replace_document(self.store, key, self.store.read(key), value)

    def _save_month(self, projection):
        window = getattr(self.store, "cache_months", None)
        if window is None or projection.month in window:
            self._save("month-" + projection.month, month_document(projection))

    def _cache_fill(self, summary):
        window = getattr(self.store, "cache_months", None)
        if window is None:
            return set()
        anchor = summary.get("cache_month")
        if not anchor:
            return set(window)
        from .monthly_projection import shift_month
        previous = {shift_month(anchor, -n) for n in range(13)}
        return set(window) - previous

    def _cache_anchor(self, summary):
        if getattr(self.store, "cache_months", None) is not None:
            summary["cache_month"] = self.store.current_month

    def bootstrap(self, category_pairs):
        """Explicit migration/rebuild while holding the existing writer lock.

        This is the only full ledger scan. IDs from a previous catalog survive a
        rebuild. Historical/blank labels remain inactive choices, never edits.
        """
        from .category_management import require_settled
        require_settled(self.store)
        old_catalog = self.store.read("catalog")
        catalog = load_catalog(old_catalog) if old_catalog else CategoryCatalog.bootstrap(category_pairs)
        catalog = self._current_categories(catalog, category_pairs)
        index, observed = LedgerIndex(), {}
        for first, rows in self.reader.bootstrap_pages():
            catalog = catalog.include_historical((str((list(r) + [""] * 7)[5]),
                                                  str((list(r) + [""] * 7)[6])) for r in rows if r and r[0])
            for number, row in enumerate(rows, first):
                index.observe(number, row, catalog)
                if row and row[0]:
                    observed[number] = row
        self._save("catalog", catalog_document(catalog))
        # Mark every month before updating any projection: a partial rebuild is
        # visibly unfinished. Repeating bootstrap uses the same catalog IDs.
        old_summary = self.store.read("summary") or {"months": {}, "coverage": {}, "required_routes": []}
        months = sorted(set(index.by_month) | set(old_summary["months"]) | set(getattr(self.store,"cache_months",())))
        self._save("journal", {"generation": (self.store.read("journal") or empty_journal())["generation"] + 1,
                               "ranges": [], "append": True, "months": months, "rebuild": True})
        summary = deepcopy(old_summary)
        for month in months:
            projection = rebuild_month(month, index, catalog,
                lambda first, last: [observed.get(n, []) for n in range(first, last + 1)])
            self._save_month(projection)
            # Empty cache slots are not proof of completed zero-spend months.
            if month in index.by_month or month in old_summary["months"]:
                summary["months"][month] = totals(projection)
        self._cache_anchor(summary)
        self._save("index", index_document(index))
        self._save("summary", summary)
        before = self.journal.read()
        replace_document(self.store, "journal", before, {**empty_journal(), "generation": before["generation"] + 1})
        # Completion is user input, kept outside disposable summary values.
        # Rebuilding a lost summary must restore it without re-importing money.
        if self.store.read("coverage") is not None:
            from .daily_coverage import Coverage
            Coverage(self.store).sync()
        return {"projection_months": len(months), "projection_rows": len(index.by_id), "errors": 0}

    @staticmethod
    def _current_categories(catalog, pairs):
        pairs = list(dict.fromkeys(pairs))
        additions = CategoryCatalog.bootstrap(pair for pair in pairs if pair not in catalog.by_name)
        active_ids={catalog.by_name[p] for p in pairs if p in catalog.by_name}
        existing=[replace(c,active=c.category_id in active_ids) for c in catalog.categories]
        return CategoryCatalog((*existing, *additions.categories))

    def refresh(self, category_pairs):
        from .category_management import require_settled
        require_settled(self.store)
        before_journal = self.journal.read()
        if before_journal.get("rebuild"):
            return self.bootstrap(category_pairs)
        before_catalog = self.store.read("catalog")
        before_summary = self.store.read("summary") if getattr(self.store,"cache_months",None) is not None else None
        cache_fill = self._cache_fill(before_summary or {})
        if not (before_journal["ranges"] or before_journal["append"] or before_journal["months"] or cache_fill):
            # Master-only edits must reach both views even without a ledger
            # mutation. Preserve IDs/aliases and never reread historical rows.
            if before_catalog is not None:
                catalog = self._current_categories(load_catalog(before_catalog), category_pairs)
                replace_document(self.store, "catalog", before_catalog, catalog_document(catalog))
            return {"projection_months": 0, "projection_rows": 0, "errors": 0}
        before_index = self.store.read("index")
        before_summary = before_summary if before_summary is not None else self.store.read("summary")
        if before_index is None or before_catalog is None or before_summary is None:
            raise ProjectionError("projection_bootstrap_required")
        index, catalog = load_index(before_index), load_catalog(before_catalog)
        catalog = self._current_categories(catalog, category_pairs)
        dirty = set(before_journal["months"]) | cache_fill
        ranges = list(before_journal["ranges"])
        if before_journal["append"]:
            first = max(index.by_row, default=1) + 1
            last = self.reader.row_count()
            if last >= first:
                ranges.append([first, last])
        ranges = list(bounded_ranges(ranges, self.reader.page_size))
        blocks = self.reader.read_ranges(ranges)
        observed_count = 0
        for (first, last), rows in zip(ranges, blocks, strict=True):
            if len(rows) != last - first + 1:
                raise ProjectionError("projection_range_incomplete")
            catalog = catalog.include_historical((str((list(r) + [""] * 7)[5]),
                                                  str((list(r) + [""] * 7)[6])) for r in rows if r and r[0])
            for number, row in enumerate(rows, first):
                dirty.update(index.observe(number, row, catalog))
                observed_count += bool(row and row[0])
        pending = {**before_journal, "months": sorted(dirty), "generation": before_journal["generation"] + 1}
        # Preserve old months even if a crash happens after the new index save.
        replace_document(self.store, "journal", before_journal, pending)
        replace_document(self.store, "catalog", before_catalog, catalog_document(catalog))
        summary = deepcopy(before_summary)
        for month in sorted(dirty):
            projection = rebuild_month(month, index, catalog, self.reader)
            self._save_month(projection)
            if month in index.by_month or month in before_summary["months"] or month not in cache_fill:
                summary["months"][month] = totals(projection)
        self._cache_anchor(summary)
        replace_document(self.store, "index", before_index, index_document(index))
        replace_document(self.store, "summary", before_summary, summary)
        replace_document(self.store, "journal", pending, {**empty_journal(), "generation": pending["generation"] + 1})
        return {"projection_months": len(dirty), "projection_rows": observed_count, "errors": 0}

    def read_month(self, month):
        month_key(month)
        window = getattr(self.store,"cache_months",None)
        if window is not None and month not in window:
            # Writer-side historical lookup only. Daily rendering reads the
            # 13 cache files directly and never invokes this ledger fallback.
            return rebuild_month(month, load_index(self.store.read("index")),
                load_catalog(self.store.read("catalog")), self.reader)
        return load_month(self.store.read("month-" + month))
