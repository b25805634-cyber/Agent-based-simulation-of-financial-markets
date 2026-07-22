# Versioned reference data

`v1/` is an immutable research input set. Corrections must create a new data
version; do not edit a historical version after it has been used in a formal
run. `v1/catalog.json` records the event identity, price convention, exact
source query, retrieval date, observation window, and t=0 rule.

New CSV artifacts use the established `timestamp,price[,news]` interface; the
loader accepts legacy `date,price[,news]` as a timestamp-column alias. Timeline
artifacts use one UTF-8 JSON object per nonblank line:

- `schema_version`: exactly `news_timeline_v1`;
- `event_id`: unique stable identifier within that file;
- `timestamp`: ISO date (a calendar/session label) or offset-aware ISO datetime;
- `public_text`: public information suitable for a social/news channel;
- `source_title`: human-readable title of the cited item;
- `source_url`: absolute HTTPS source URL;
- `source_published_date`: date-only ISO value reported by the source.

No field is available for private rationale. Consumers must not infer or add
private agent state to these public timeline records.

Use `load_reference_episode(csv_path)` for validated CSV-only loading. Timeline
semantics require both the exact JSONL path and
`include_news_timeline=True`; sibling-file discovery is intentionally absent.
The central simulator and its CLI do not inject these timelines in Wave1-T1.
Loaded events expose a descriptive `price_anchor_t` and a conservative
non-backward `delivery_t`; only the latter is eligible for a future injection
driver. Date-only and after-16:00 New York events roll to the next observed
session.
