# Pinned dependencies

## spaCy entity-extraction models (item 32)

`entity-scan` (docs/IMPROVEMENTS.md item 32) needs a real trained NER model, not a regex
heuristic - a low-quality regex "NER" would flag every capitalized phrase as an entity,
cluttering search results with noise on a tool whose whole premise is trustworthy answers.
spaCy was chosen over Elasticsearch's own hosted-model inference API (which needs a separate
Eland-based upload workflow, outside this project's docker-compose pattern) after the choice
was explicitly raised for confirmation, per this project's minimize-dependencies policy - a
new dependency of this size (spaCy itself pulls in ~24 packages, including numpy/thinc) isn't
picked unilaterally.

Both the `spacy` package and its two per-language model wheels (not on PyPI's index - fetched
directly from GitHub Releases) are pinned exactly in `bin/pyproject.toml`/`uv.lock`, with `uv`
recording each wheel's own sha256 in `uv.lock` from the actual download - the same integrity
guarantee `--hash`-pinning a `requirements.txt` line gives, just uv-native. "sm" (small) tier
for both languages, not "md"/"lg": real trained NER without the much larger word-vector data
those tiers add, which entity extraction doesn't need.

- Source: <https://pypi.org/project/spacy/>, <https://github.com/explosion/spacy-models>
- spaCy version: 3.8.16
- `en_core_web_sm` version: 3.8.0, sha256
  `1932429db727d4bff3deed6b34cfc05df17794f4a52eeb26cf8928f7c1a0fb85`
- `sv_core_news_sm` version: 3.8.0, sha256
  `be4929fb30523dca0b6672f999cdbf4d64f165419f1eed0014ca3a36599b8b4d`
- Pinned on: 2026-09-09

To upgrade: read spaCy's own changelog for the new version, check what its dependency chain
(`thinc`/`numpy`/`cryptography`-adjacent native deps) pulls in differently, confirm both
model wheels have a matching release for the new spaCy minor version at
<https://github.com/explosion/spacy-models/releases> (model versions track spaCy's own, e.g.
`en_core_web_sm-3.8.x` needs `spacy>=3.8.0,<3.9.0`), then `uv add "spacy==<version>"` and
re-pin the two model wheel URLs in `bin/pyproject.toml`'s `[tool.uv.sources]` before running
`uv lock` / regenerating `bin/requirements.txt`.

## City database (`bin/cities.tsv`, `deis geo-report`'s nearest-city lookup)

`bin/geo.py`'s `nearest_city()` needs a local database of real place names to answer "what
city is this GPS point near" without a network call (the whole point - `deis geo-scan`'s own
GPS points come from photo EXIF data, and looking them up against a live geocoding API would
send a leak case's coordinates to a third party for every report run). Not written by hand:
world city name/coordinate/population data is exactly upstream GeoNames data
(<https://www.geonames.org/>, CC BY 4.0), which `download.geonames.org` itself wasn't reachable
from this project's normal environment for - reached instead through the `geonamescache` PyPI
package (<https://pypi.org/project/geonamescache/>), which bundles the same GeoNames extract.
Only 5 of that package's per-city columns are kept (name, latitude, longitude, country code,
population) - not `geonamescache`'s own ~30-entry `alternatenames` list per city, which this
has no use for and would multiply the committed file size for nothing.

Filtered to population >= 100,000 (6,204 of ~34,000 GeoNames cities) - low enough that most
populated regions still have a nearby match, high enough that the answer is always a place a
reader recognizes by name rather than the technically-nearest small town ("nearest *big*
city," not "nearest city"). Not re-fetched automatically - regenerate with:

```bash
uv run --with geonamescache python3 -c "
import geonamescache
gc = geonamescache.GeonamesCache()
cities = [c for c in gc.get_cities().values() if c['population'] >= 100000]
cities.sort(key=lambda c: c['name'])
with open('bin/cities.tsv', 'w', encoding='utf-8') as f:
    f.write('#name\tlat\tlon\tcountry\tpopulation\n')
    for c in cities:
        name = c['name'].replace('\t', ' ')
        f.write(f\"{name}\t{c['latitude']}\t{c['longitude']}\t{c['countrycode']}\t{c['population']}\n\")
"
```

- Source: <https://pypi.org/project/geonamescache/> (GeoNames data, CC BY 4.0)
- `geonamescache` version used to generate: 3.0.2
- Generated on: 2026-09-11
