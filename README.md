# Diamond Move market data (Hounslow)

Free, self-owned replacement for the Bricks and Logic market widget on
https://www.diamondmove.co.uk/hounslow-property-prices. It covers the same
ground (key figures, a map of the towns, price changes over 3 months, 1, 3 and
5 years, rents and yield, recent sales) in Diamond Move's own design. The site's
own call to action sits under it.

Once a month a GitHub Action downloads HM Land Registry Price Paid Data, the
UK House Price Index for Hounslow and its neighbouring boroughs, and the ONS
Price Index of Private Rents, works out the figures, and commits:

- `data/hounslow.json`: the figures the page widget reads
- `data/hounslow-seo.html` and `data/hounslow-seo-<town>.html`: the written
  report as plain HTML. The widget already puts this text into the page every
  month (under "Read the full market report"), so nobody has to paste it; the
  files are there if Estates Online can include them on the server instead
- `data/hounslow-summary.txt`: a single summary paragraph, with the attribution lines

## What it calculates

| Area | Headline price and annual change | Sales, median, by type, recent sales |
|---|---|---|
| Hounslow borough | UK House Price Index (official, mix-adjusted average) | Price Paid Data, district = HOUNSLOW |
| TW3, TW4, TW5, TW7 | Price Paid median, last 12 months vs previous 12 | Price Paid Data, postcode district |

| Postcode districts TW3, TW4, TW5, TW7, TW8, TW13, TW14, W4 | Change in median price over rolling 12-month windows, for the period maps and best/worst cards | Price Paid Data; map positions from postcodes.io |
| Hounslow, Ealing, Hillingdon, Richmond upon Thames, Spelthorne | Average price and change over 3 months, 1, 3 and 5 years | UK HPI |
| The same five boroughs | Average rent (overall and by bedrooms), change over each period, gross yield | ONS Price Index of Private Rents |

Notes on method:

- The borough map is drawn from where homes have sold: every sale postcode is
  located with postcodes.io, those inside the borough are placed on a 100 m
  grid, and each grid square takes the district of the nearest postcode (empty
  where none is within 300 m, such as parks). The shapes are close to, but not
  exactly, the official boundaries. Districts with fewer than 20 sales in a
  12-month window have no median.
- Gross yield is the ONS average rent × 12 divided by the UK HPI average price.
- ONS publishes rents for local authorities only, so lettings are compared
  across neighbouring boroughs, not postcodes.
- ONS refuses downloads unless the user agent starts with "Mozilla/5.0"; the
  script sets one that also names Diamond Move.

- Only standard residential sales count (Price Paid category A, property types
  D/S/T/F). Category B (repossessions, buy-to-let portfolio sales, transfers
  to companies) and type O are left out, as the UK HPI does.
- The newest month in Price Paid Data is always partial, so the 12-month
  window ends one month earlier (`drop_latest_months` in the config). Recent
  sales still show the newest sales.
- The UK HPI isn't published for postcode districts, so postcodes use the
  median sale price. Postcode figures from a few dozen sales move around
  more than the borough figure. Types with fewer than 3 sales are hidden.
- Postcode districts don't follow borough boundaries exactly, so postcode
  figures include every sale in that postcode district.

## Setup

1. Create a GitHub repository (public is simplest, it's free), e.g. `market-data`,
   and push this folder to it.
2. Settings → Actions → General → Workflow permissions: allow read and write.
3. Actions tab → "Monthly market data" → Run workflow. It runs again on the
   25th of each month. It takes a few minutes (four yearly CSVs, ~100–200 MB each).
4. Point the widget at the file: in `embed/hounslow-market-embed.html` set
   `data-src` to `https://cdn.jsdelivr.net/gh/manrajbisran/market-data@main/data/hounslow.json`
   (or GitHub Pages: `https://manrajbisran.github.io/market-data/data/hounslow.json`).
5. Brand colours are the `--dm-*` variables at the top of the widget's `<style>`.
6. Home page "Market insights" section: paste the embed block with
   `data-town="compare"`. It shows every town side by side; clicking a town
   opens its area guide (the `url` in `config/hounslow.json`).
7. Each area guide: paste the same block with `data-town` set to that town's
   id, e.g. `data-town="isleworth"`. Each guide then has its own figures, which
   is what lets it rank for "<town> house prices".
8. Optional: with `data-town=""` the block is a single page with a tab per town
   and "Compare all towns" last; tabs can be linked with `#isleworth` or `#compare`.

## Towns

Each town in `config/hounslow.json` is a list of council wards (as named by
postcodes.io, which uses the ONS ward names). A sale counts for a town when its
postcode is in one of those wards. Current towns: Hounslow, Heston, Isleworth,
Whitton (Richmond upon Thames), Feltham, Osterley, Cranford and Brentford.
To add a town, add an entry with an `id`, `name`, `wards`, `url` and, if it is
outside Hounslow, `borough` (and its outcode under `extra_outcodes`). Towns with
fewer than 20 sales in 12 months get no median.

If a source is down the job fails and the previous month's file stays live.

Run locally (set `PPD_CACHE_DIR=some/folder` to keep the Price Paid files between runs):
`pip install -r requirements.txt`, then
`python3 scripts/build_market_data.py --config config/hounslow.json --out data/hounslow.json --summary data/hounslow-summary.txt --seo-html data/hounslow-seo.html`
Tests (offline, made-up rows): `python3 -m unittest discover -s tests`

Adding another area later: copy `config/hounslow.json`, change the district,
outcodes, `hpi_region` (the UK HPI slug, e.g. `ealing`), `rent_area` (the ONS
area name), `districts` and `neighbours`, and add a step to the workflow.

## Attribution (required by the Open Government Licence)

Show this wherever the figures appear (the widget prints it automatically):

> Contains HM Land Registry data © Crown copyright and database right 2026.
> This data is licensed under the Open Government Licence v3.0.

and for the rents:

> Private rents: Office for National Statistics, Price Index of Private Rents,
> licensed under the Open Government Licence v3.0.

and for the House Price Index figures:

> UK House Price Index: HM Land Registry, Office for National Statistics,
> Registers of Scotland and Land & Property Services Northern Ireland,
> licensed under the Open Government Licence v3.0.

Link "Open Government Licence v3.0" to
https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/.
The year updates automatically each run.

Price Paid addresses come from Ordnance Survey AddressBase / Royal Mail PAF.
HM Land Registry allows them to be displayed to provide residential property
price information, which is what the recent-sales table does. Don't extract
them for marketing lists.
