# Example: Recovery BI

A Power BI model of Amazon vendor deduction findings from [recovery-bi](https://github.com/jmnzcn/recovery-bi):
5 tables, 14 measures, synthetic data. [`dax-parity.toml`](dax-parity.toml) writes each measure in
SQL and tests it on 7 slices, 18,159 cells in all. [`parity/dax_results.json`](parity/dax_results.json)
was recorded in Power BI Desktop, and [`parity/mutation_report.txt`](parity/mutation_report.txt) is the
latest mutation run: 46 of 46 mutants caught, 1 marked equivalent.

```bash
cd examples/recovery-bi
dax-parity check     # compares the recorded results with the SQL reference
dax-parity run       # on Windows with Power BI Desktop: record again, then check
dax-parity mutate    # on Windows: break each measure on purpose and count what the checks catch
```
