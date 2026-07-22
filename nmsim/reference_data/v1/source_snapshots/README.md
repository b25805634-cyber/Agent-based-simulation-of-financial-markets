# Nasdaq extraction evidence

Each JSONL file preserves the complete date/close/volume/open/high/low objects
returned for the catalog's inclusive event window on 2026-07-22. The catalog
also records the SHA-256 and byte length of the larger raw API response before
filtering, its total record count, and a canonical `ISO-date,close` digest.

The exact request intentionally omits `todate`: on the retrieval date, adding a
`todate` parameter produced a successful status with zero historical rows. The
response selected by `fromdate&limit=5000` is filtered locally and sorted in
ascending session order. These snapshots are provenance evidence, not separate
experiment inputs; the corresponding CSV bytes are the price-path inputs.
