# dax-parity

[![ci](https://github.com/jmnzcn/dax-parity/actions/workflows/ci.yml/badge.svg)](https://github.com/jmnzcn/dax-parity/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB)
![License](https://img.shields.io/badge/license-Apache%202.0-blue)

Tests every DAX measure in a Power BI model against an independent calculation, cell by cell, using
Power BI's own engine.

You write each measure a second time as plain SQL over the same data. dax-parity runs both: the DAX
inside Power BI Desktop, the SQL in DuckDB. It compares every measure across every slice you choose
(totals, by product, by customer, by month, by customer and product) and tells you which cells differ
and why. On the example in this repo that is 17,885 cells in about 30 seconds.

## Why

Most DAX tests are a handful of hand-written assertions: "total sales should be 1,234,567". They
catch a broken total. They don't catch a measure that is right overall and wrong for 56 of 1,188
customer and product pairs, which is the kind of bug that reaches a report.

Here is a real one, from the model in [`examples/recovery-bi`](examples/recovery-bi). `Claim Rate`
was written as `DIVIDE ( [Claims], [Total Findings] )`. Where a vendor had findings in a category
but no claims, `[Claims]` is BLANK, and `DIVIDE(BLANK, n)` is BLANK, so the report showed an empty
cell where the answer is 0 %. Totals were fine, so nobody would have noticed. dax-parity:

```
  by_status                6 rows       78 cells      5 mismatches
  by_vendor_category    1188 rows    15444 cells     51 mismatches
17885 cells compared, 56 mismatches

by measure:
  Claim Rate: 56 of 1373 cells (56 blank_vs_zero)

first differences:
  by_status [EXPIRED] Claim Rate: Power BI BLANK, expected 0.0  [blank_vs_zero]
  by_vendor_category [1F98O, UNDERPAYMENT] Claim Rate: Power BI BLANK, expected 0.0  [blank_vs_zero]
  ...

hint (blank_vs_zero): BLANK and 0 differ. If the answer should be 0, add + 0 or COALESCE ( ..., 0 )
to the DAX; if it should be blank, set blank_if_zero = true on the measure.
```

The full output is in [docs/claim-rate-bug.txt](docs/claim-rate-bug.txt). With `[Claims] + 0` the
same check gives 0 mismatches.

This is differential testing: two independent implementations of the same thing, compared on a lot
of inputs. It works because SQL and DAX fail differently. A wrong filter context, a relationship
going the wrong way, BLANK handling, a `DIVIDE` that hides a zero: SQL doesn't share those mistakes,
so they show up as differences.

## How it works

1. `dax-parity record` (Windows) copies your `.pbip` to a scratch folder, points its parameters at
   your data, opens it in Power BI Desktop, refreshes it, and runs one DAX query per slice through
   Desktop's Analysis Services engine. The results are saved to a JSON file you commit.
2. `dax-parity check` (anywhere, including CI on Linux) computes the same slices in DuckDB from your
   SQL, compares every cell and exits with 1 if anything differs.
3. The recorded file includes a hash of each measure's DAX, each data file and each query. If someone
   edits a measure and doesn't record again, `check` fails with "measure 'X' changed since the
   results were recorded" instead of comparing old numbers.

Each difference gets a kind, because the kind usually says what to fix: `blank_vs_zero`, `value`
(both are numbers and differ by more than the tolerance), `missing_row` (the reference has a row
Power BI didn't return) or `extra_row`.

## Quick start

```bash
pip install dax-parity
dax-parity init path/to/Model.pbip      # writes dax-parity.toml with every measure listed
```

Then fill in the SQL for each measure and the slices you care about:

```toml
[model]
pbip = "powerbi/Sales.pbip"
parameters = { DataFolder = "{root}/data" }   # optional: what the scratch copy loads from

[reference]
tables = { Sales = "data/sales.parquet", Product = "data/product.parquet" }
from = "Sales JOIN Product USING (ProductKey)"

[[measure]]
name = "Revenue"
sql = "SUM(Sales.Amount)"
compare = "money"          # exact, money (half a cent) or ratio (1e-9); or tolerance = 0.01

[[measure]]
name = "Orders"
sql = "COUNT(DISTINCT Sales.OrderId)"
compare = "exact"
blank_if_zero = true       # COUNT gives 0 where DAX gives BLANK

[[slice]]
id = "totals"

[[slice]]
id = "by_category"
by = ["'Product'[Category]"]
```

dax-parity writes the `GROUP BY` for each slice. A slice that needs something else, like a running
total, can bring its own SQL; see the `by_month` slice in
[the example](examples/recovery-bi/dax-parity.toml). If you'd rather write the reference in Python,
set `python = "module:function"` under `[reference]` and return rows (or DataFrames) per slice.

```bash
dax-parity record          # Windows, with Power BI Desktop and the .NET SDK installed
dax-parity check           # anywhere; --junit report.xml for CI test reports
dax-parity run             # both
dax-parity queries         # print the DAX it will run
dax-parity validate        # check the TMDL with Microsoft's Tabular Object Model
```

Exit codes: 0 everything matches, 1 differences, 2 configuration error, 3 recorded results out of
date.

## In CI

Record on a Windows machine when measures or data change, commit the results, and let CI check:

```yaml
- run: pip install dax-parity
- run: dax-parity check --junit parity.junit.xml
```

[This repo's own workflow](.github/workflows/ci.yml) does exactly that on the example, on Linux,
and also validates the model's TMDL with Microsoft's Tabular Object Model.

## Requirements and limits

- Recording needs Windows, Power BI Desktop and the .NET 8 SDK or later. The small .NET runner is
  built on first use. Checking only needs Python 3.11+.
- It tests models saved as Power BI Projects (`.pbip`, TMDL format), not `.pbix` files.
- The reference has to be written by someone who didn't just translate the DAX line by line, or
  it will repeat the same mistakes. Writing the SQL from the business definition is the point.
- Slices filter by grouping columns. Testing under specific slicer selections or row-level security
  isn't supported yet.

## Roadmap

The open-source version stays focused on Power BI Desktop and recorded results. I'm working on a
commercial edition for teams: running the queries directly against Fabric and Premium workspaces
over XMLA (no Windows machine or recording step), service principal setups for Azure DevOps and
GitHub Actions, and HTML reports for audits. If that would help your team, write to
[neyfred@gmail.com](mailto:neyfred@gmail.com).

## Related tools

[pql-test](https://pypi.org/project/pql-test/) and [pbi-unit-test](https://pypi.org/project/pbi-unit-test/)
run DAX assertions with expected values you write by hand, and
[Tabular Editor's Best Practice Analyzer](https://docs.tabulareditor.com/) checks how a model is
built. dax-parity is complementary: it doesn't need you to know the right answers, only a second
way of computing them.

## License

[Apache 2.0](LICENSE). The example model and its synthetic data come from
[recovery-bi](https://github.com/jmnzcn/recovery-bi) and are included here under the same license.

Ney Fred Jiménez Campos · [LinkedIn](https://www.linkedin.com/in/neyfred/) · [GitHub](https://github.com/jmnzcn)
