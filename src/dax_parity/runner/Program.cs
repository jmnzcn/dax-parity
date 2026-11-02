// DaxRunner: the .NET side of dax-parity, built on first use with `dotnet build`.
//
//   validate <SemanticModel/definition>       parse the TMDL with Microsoft's serializer and validate the model
//   ping <port>                               print how many databases the engine on that port has loaded
//   refresh <port>                            full refresh of the model open in Power BI Desktop
//   query <port> <queries.json> <out.json>    run each DAX query and write its rows as JSON
//   mutate <port> <mutants.json> <out.json>   for each mutant: change one measure, run its queries, restore it
//
// "validate" runs anywhere, including CI on Linux. The others talk to the Analysis Services engine
// that Power BI Desktop starts on localhost for an open report, so they need Windows.

using System.Text.Json;
using System.Text.Json.Nodes;
using Microsoft.AnalysisServices.AdomdClient;
using Microsoft.AnalysisServices.Tabular;

return args switch
{
    ["validate", var folder] => Validate(folder),
    ["ping", var port] => Ping(int.Parse(port)),
    ["refresh", var port] => Refresh(int.Parse(port)),
    ["query", var port, var queries, var output] => Query(int.Parse(port), queries, output),
    ["mutate", var port, var mutants, var output] => Mutate(int.Parse(port), mutants, output),
    _ => Usage(),
};

static int Usage()
{
    Console.Error.WriteLine("usage: DaxRunner validate <definition> | ping <port> | refresh <port> | query|mutate <port> <in.json> <out.json>");
    return 2;
}

static int Validate(string folder)
{
    Database db;
    try
    {
        db = TmdlSerializer.DeserializeDatabaseFromFolder(folder);
    }
    catch (Exception e) when (e.GetType().Name.StartsWith("Tmdl"))
    {
        Console.Error.WriteLine($"TMDL parse error: {e.Message}");
        return 1;
    }
    var model = db.Model;
    var result = model.Validate();
    var measures = model.Tables.SelectMany(t => t.Measures).Count();
    var columns = model.Tables.SelectMany(t => t.Columns).Count(c => c.Type != ColumnType.RowNumber);
    Console.WriteLine($"compatibility {db.CompatibilityLevel}: {model.Tables.Count} tables, {columns} columns, " +
                      $"{measures} measures, {model.Relationships.Count} relationships");
    foreach (var e in result.Errors) Console.Error.WriteLine($"error: {e.Message}");
    return result.Errors.Count == 0 ? 0 : 1;
}

static int Ping(int port)
{
    using var server = new Server();
    server.Connect($"Data Source=localhost:{port}");
    Console.WriteLine(server.Databases.Count);
    return 0;
}

static int Refresh(int port)
{
    using var server = new Server();
    server.Connect($"Data Source=localhost:{port}");
    var db = server.Databases[0];
    var sw = System.Diagnostics.Stopwatch.StartNew();
    db.Model.RequestRefresh(RefreshType.Full);
    db.Model.SaveChanges();
    Console.WriteLine($"refreshed {db.Model.Tables.Count} tables in {sw.Elapsed.TotalSeconds:F1} s");
    return 0;
}

static int Query(int port, string queriesPath, string outPath)
{
    var queries = JsonNode.Parse(File.ReadAllText(queriesPath))!.AsArray();
    using var conn = new AdomdConnection($"Data Source=localhost:{port}");
    conn.Open();
    var results = new JsonObject
    {
        ["engine"] = $"Power BI Desktop (Analysis Services {conn.ServerVersion})",
        ["recorded_utc"] = DateTime.UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ"),
    };
    var outQueries = new JsonObject();
    foreach (var q in queries)
    {
        var id = q!["id"]!.GetValue<string>();
        var dax = q["dax"]!.GetValue<string>();
        var rows = Rows(conn, dax);
        outQueries[id] = new JsonObject { ["dax"] = dax, ["rows"] = rows };
        Console.WriteLine($"{id}: {rows.Count} rows");
    }
    results["queries"] = outQueries;
    File.WriteAllText(outPath, results.ToJsonString(new JsonSerializerOptions { WriteIndented = true }) + "\n");
    return 0;
}

static JsonArray Rows(AdomdConnection conn, string dax)
{
    using var cmd = new AdomdCommand(dax, conn);
    using var reader = cmd.ExecuteReader();
    var rows = new JsonArray();
    while (reader.Read())
    {
        var row = new JsonObject();
        for (var i = 0; i < reader.FieldCount; i++)
        {
            var v = reader.GetValue(i);
            row[reader.GetName(i)] = v switch
            {
                null or DBNull => null,
                DateTime d => JsonValue.Create(d.TimeOfDay == TimeSpan.Zero ? d.ToString("yyyy-MM-dd") : d.ToString("s")),
                decimal m => JsonValue.Create(m),
                double x => JsonValue.Create(x),
                long l => JsonValue.Create(l),
                int n => JsonValue.Create(n),
                bool b => JsonValue.Create(b),
                _ => JsonValue.Create(v.ToString()),
            };
        }
        rows.Add(row);
    }
    return rows;
}

// Each mutant changes one measure's expression in the engine, runs only the queries that can see the
// change, and puts the original expression back, so the model always ends as it started.
static int Mutate(int port, string mutantsPath, string outPath)
{
    var mutants = JsonNode.Parse(File.ReadAllText(mutantsPath))!.AsArray();
    using var server = new Server();
    server.Connect($"Data Source=localhost:{port}");
    var model = server.Databases[0].Model;
    var byName = model.Tables.SelectMany(t => t.Measures).ToDictionary(m => m.Name);
    var output = new JsonArray();
    var n = 0;
    foreach (var mutant in mutants)
    {
        n++;
        var id = mutant!["id"]!.GetValue<string>();
        var name = mutant["measure"]?.GetValue<string>();
        var expression = mutant["expression"]?.GetValue<string>();
        var result = new JsonObject { ["id"] = id };
        Microsoft.AnalysisServices.Tabular.Measure? target = null;
        string? original = null;
        var sw = System.Diagnostics.Stopwatch.StartNew();
        try
        {
            if (name is not null && expression is not null)
            {
                if (!byName.TryGetValue(name, out target)) throw new InvalidOperationException($"measure {name} not found");
                original = target.Expression;
                target.Expression = expression;
                model.SaveChanges();
            }
            using var conn = new AdomdConnection($"Data Source=localhost:{port}");
            conn.Open();
            var queries = new JsonObject();
            foreach (var q in mutant["queries"]!.AsArray())
                queries[q!["id"]!.GetValue<string>()] = new JsonObject { ["rows"] = Rows(conn, q["dax"]!.GetValue<string>()) };
            result["queries"] = queries;
        }
        catch (Exception e)
        {
            result["error"] = e.Message.Split('\n')[0];
            model.UndoLocalChanges();
        }
        finally
        {
            if (target is not null && original is not null && target.Expression != original)
            {
                target.Expression = original;
                model.SaveChanges();
            }
        }
        result["seconds"] = Math.Round(sw.Elapsed.TotalSeconds, 2);
        output.Add(result);
        Console.Error.WriteLine($"[{n}/{mutants.Count}] {id}{(result["error"] is null ? "" : " (error)")}");
    }
    File.WriteAllText(outPath, output.ToJsonString(new JsonSerializerOptions { WriteIndented = true }) + "\n");
    return 0;
}
