# Example: Recovery BI

A Power BI model of Amazon vendor deduction findings from [recovery-bi](https://github.com/jmnzcn/recovery-bi):
5 tables, 14 measures, synthetic data. [`dax-parity.toml`](dax-parity.toml) writes each measure in
SQL and tests it on 6 slices, 17,885 cells in all. [`parity/dax_results.json`](parity/dax_results.json)
was recorded in Power BI Desktop.

```bash
cd examples/recovery-bi
dax-parity check     # compares the recorded results with the SQL reference
dax-parity run       # on Windows with Power BI Desktop: record again, then check
```
