# AGENTS.md: {title}

{rows} from the CBS Wijk- en Buurtkaart {year}. The key is `({code}, water)`.{parents} The conventions and the cross-year recipe are in [../../AGENTS.md](../../AGENTS.md).

## Queries

```sql
INSTALL spatial; LOAD spatial;
-- Rows by land/water
SELECT water, count(*) FROM read_parquet('{url}') GROUP BY water;
-- {counts}

{area}
```
