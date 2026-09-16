using System.Reflection;
using System.Runtime.Loader;
using System.Net;
using System.Security.Cryptography;
using System.Text.Json;

string directory = args[0];
AssemblyLoadContext.Default.Resolving += (_, name) => {
    string path = Path.Combine(directory, name.Name + ".dll");
    return File.Exists(path) ? AssemblyLoadContext.Default.LoadFromAssemblyPath(path) : null;
};
var assemblies = new Dictionary<string, Assembly>();
foreach (string name in new[] {
    "Microsoft.VisualStudio.Services.BlobStore.Common",
    "Microsoft.VisualStudio.Services.BlobStore.WebApi",
    "Microsoft.VisualStudio.Services.Content.Common"
}) {
    Assembly assembly = AssemblyLoadContext.Default.LoadFromAssemblyPath(
        Path.Combine(directory, name + ".dll"));
    assemblies[name] = assembly;
    if (args.Length > 1)
        continue;
    foreach (Type type in assembly.GetExportedTypes().Where(t =>
        t.Name.Contains("Compressed") || t.Name.Contains("Header")
        || t.Name == "DedupStoreHttpClient" || t.Name == "IDedupStoreHttpClient")) {
        Console.WriteLine(type.FullName);
        foreach (var member in type.GetMembers(BindingFlags.Public | BindingFlags.Instance
            | BindingFlags.Static | BindingFlags.DeclaredOnly))
            Console.WriteLine("  " + member);
    }
}
    if (args.Length > 1 && args[1] == "vectors") {
        var hashing = AssemblyLoadContext.Default.LoadFromAssemblyPath(
            Path.Combine(directory, "BuildXL.Cache.ContentStore.Hashing.dll"));
        var algorithmType = hashing.GetType("BuildXL.Cache.ContentStore.Hashing.DedupNodeOrChunkHashAlgorithm")!;
        byte[] seeded = new byte[3 * 1048576];
        byte[] seed = System.Text.Encoding.UTF8.GetBytes("az-artifacts local chunker vectors v1\0");
        byte[] input = new byte[seed.Length + 8];
        seed.CopyTo(input, 0);
        for (int i = 0; i < seeded.Length / 32; ++i) {
            System.Buffers.Binary.BinaryPrimitives.WriteUInt64LittleEndian(input.AsSpan(seed.Length), (ulong)i);
            SHA256.HashData(input).CopyTo(seeded, i * 32);
        }
        var cases = new List<(string Name, (string Kind, int Length)[] Parts)>();
        foreach (int size in new[] { 0, 1, 32767, 32768, 32769, 131071, 131072, 131073,
            1048575, 1048576, 1048577, 2097171 }) {
            cases.Add(("zeros-" + size, new[] { ("zeros", size) }));
            cases.Add(("seeded-" + size, new[] { ("seeded", size) }));
        }
        foreach (int zeroLength in new[] { 15, 16, 32767, 32768, 131072, 1048576 }) {
            cases.Add(("transition-" + zeroLength, new[] {
                ("seeded", 196619), ("zeros", zeroLength), ("seeded", 1048591)
            }));
        }
        var output = new List<object>();
        foreach (var test in cases) {
            using var stream = new MemoryStream();
            foreach (var part in test.Parts)
                stream.Write(part.Kind == "zeros" ? new byte[part.Length] : seeded.AsSpan(0, part.Length));
            byte[] data = stream.ToArray();
            using var hasher = (HashAlgorithm)Activator.CreateInstance(algorithmType)!;
            byte[] root = hasher.ComputeHash(data);
            object node = algorithmType.GetMethod("GetNode")!.Invoke(hasher, null)!;
            var leafs = (System.Collections.IEnumerable)node.GetType()
                .GetMethod("EnumerateChunkLeafsInOrder")!.Invoke(node, null)!;
            var chunks = new List<object>();
            foreach (object leaf in leafs)
                chunks.Add(new {
                    id = Convert.ToHexString((byte[])leaf.GetType().GetField("Hash")!.GetValue(leaf)!) + "01",
                    size = (ulong)leaf.GetType().GetField("TransitiveContentBytes")!.GetValue(leaf)!
                });
            output.Add(new { name = test.Name,
                recipe = test.Parts.Select(p => new { kind = p.Kind, length = p.Length }),
                sha256 = Convert.ToHexString(SHA256.HashData(data)).ToLowerInvariant(),
                root = Convert.ToHexString(root), chunks });
        }
        Console.WriteLine(JsonSerializer.Serialize(new {
            source = "ArtifactTool 0.2.574 installed public BuildXL hashing API; local synthetic inputs only",
            seed = "az-artifacts local chunker vectors v1",
            recipe = "seeded parts restart SHA256(UTF8(seed) + NUL + uint64_le(i)) at i=0; zero parts contain zero bytes",
            vectors = output
        }, new JsonSerializerOptions { WriteIndented = true }));
        return;
    }
    if (args.Length > 1) {
        var common = assemblies["Microsoft.VisualStudio.Services.BlobStore.Common"];
        var web = assemblies["Microsoft.VisualStudio.Services.BlobStore.WebApi"];
        var clientType = web.GetType("Microsoft.VisualStudio.Services.BlobStore.WebApi.DedupStoreHttpClient")!;
        var bufferType = common.GetType("Microsoft.VisualStudio.Services.BlobStore.Common.DedupCompressedBuffer")!;
        var keepType = common.GetType("Microsoft.VisualStudio.Services.BlobStore.Common.KeepUntilBlobReference")!;
        var receiptType = common.GetType("Microsoft.VisualStudio.Services.BlobStore.Common.KeepUntilReceipt")!;
        var summaryType = common.GetType("Microsoft.VisualStudio.Services.BlobStore.Common.SummaryKeepUntilReceipt")!;
        object client = clientType.GetConstructor(new[] { typeof(Uri), typeof(HttpMessageHandler), typeof(bool) })!
            .Invoke(new object[] { new Uri("https://offline.invalid/"), new LocalHandler(), true });
        object keep = Activator.CreateInstance(keepType, new DateTime(2030, 1, 2, 3, 4, 5, DateTimeKind.Utc))!;
        byte[] raw = System.Text.Encoding.ASCII.GetBytes("abc");
        object buffer = bufferType.GetMethod("FromUncompressed", new[] { typeof(byte[]) })!
            .Invoke(null, new object[] { raw })!;
        object id = bufferType.GetProperty("ChunkIdentifier")!.GetValue(buffer)!;
        async Task Invoke(string method, object[] values) {
            try { await (Task)clientType.GetMethod(method)!.Invoke(client, values)!; }
            catch (InvalidOperationException) { Console.WriteLine("local-request-captured"); }
        }
        await Invoke("PutChunkAndKeepUntilReferenceAsync", new[] { id, buffer, keep, CancellationToken.None });
        var buffers = (System.Collections.IDictionary)Activator.CreateInstance(
            typeof(Dictionary<,>).MakeGenericType(id.GetType(), bufferType))!;
        buffers.Add(id, buffer);
        object other = bufferType.GetMethod("FromUncompressed", new[] { typeof(byte[]) })!
            .Invoke(null, new object[] { System.Text.Encoding.ASCII.GetBytes("defgh") })!;
        buffers.Add(bufferType.GetProperty("ChunkIdentifier")!.GetValue(other)!, other);
        await Invoke("PutChunksAsync", new object[] { buffers, keep, CancellationToken.None });
        byte[] node = new byte[40];
        node[5] = 3;
        SHA512.HashData(raw).AsSpan(0, 32).CopyTo(node.AsSpan(8));
        object nodeBuffer = bufferType.GetMethod("FromUncompressed", new[] { typeof(byte[]) })!
            .Invoke(null, new object[] { node })!;
        var hashType = AssemblyLoadContext.Default.LoadFromAssemblyPath(
            Path.Combine(directory, "BuildXL.Cache.ContentStore.Hashing.dll"))
            .GetType("BuildXL.Cache.ContentStore.Hashing.HashType")!;
        object nodeId = bufferType.GetMethod("NodeIdentifier")!
            .Invoke(nodeBuffer, new[] { Enum.Parse(hashType, "Dedup64K") })!;
        Array receipts = Array.CreateInstance(receiptType, 1);
        receipts.SetValue(Activator.CreateInstance(receiptType, keep, new byte[32]), 0);
        object summary = Activator.CreateInstance(summaryType, receipts)!;
        await Invoke("PutNodeAndKeepUntilReferenceAsync",
            new[] { nodeId, nodeBuffer, keep, summary, CancellationToken.None });
        Array pair = Array.CreateInstance(receiptType, 2);
        byte[] first = Enumerable.Range(0, 32).Select(i => (byte)i).ToArray();
        byte[] second = Enumerable.Range(31, 32).Select(i => (byte)i).ToArray();
        object later = Activator.CreateInstance(keepType,
            new DateTime(2030, 1, 3, 3, 4, 5, DateTimeKind.Utc))!;
        pair.SetValue(Activator.CreateInstance(receiptType, keep, first), 0);
        pair.SetValue(Activator.CreateInstance(receiptType, later, second), 1);
        object pairSummary = Activator.CreateInstance(summaryType, pair)!;
        byte[] aggregate = (byte[])summaryType.GetField("Signature")!.GetValue(pairSummary)!;
        Console.WriteLine(JsonSerializer.Serialize(new {
            source = "synthetic receipts only",
            summarySignatureIsXor = aggregate.SequenceEqual(first.Zip(second, (a,b) => (byte)(a^b))),
            summarySignatureIsSum = aggregate.SequenceEqual(first.Zip(second, (a,b) => (byte)(a+b))),
            syntheticAggregateLength = aggregate.Length,
            summarySignatureIsOrderedSha256 = aggregate.SequenceEqual(
                SHA256.HashData(first.Concat(second).ToArray())),
            singleSummaryIsSha256 = ((byte[])summaryType.GetField("Signature")!.GetValue(summary)!)
                .SequenceEqual(SHA256.HashData(new byte[32]))
        }));
        await Invoke("PutNodeAndKeepUntilReferenceAsync",
            new[] { nodeId, nodeBuffer, keep, pairSummary, CancellationToken.None });
    }

    sealed class LocalHandler : HttpMessageHandler
    {
        protected override async Task<HttpResponseMessage> SendAsync(HttpRequestMessage request,
            CancellationToken token)
        {
            if (request.Method == HttpMethod.Options) {
                return new HttpResponseMessage(HttpStatusCode.OK) { RequestMessage = request,
                    Content = new StringContent(
                    JsonSerializer.Serialize(new { count = 2, value = new[] { "chunks", "nodes" }
                    .Select(kind => new {
                        id = kind == "nodes" ? "53e6e1e0-7444-47ea-93cd-44e6ddf264e6"
                            : "c8911095-ce13-48e9-b1dc-158c716aa6ba",
                        area = "dedup", resourceName = kind,
                        routeTemplate = "_apis/{area}/{resource}/{dedupId}",
                        resourceVersion = 1, minVersion = "1.0", maxVersion = "7.2", releasedVersion = "0.0"
                    }) }), System.Text.Encoding.UTF8, "application/json") };
            }
            var headers = request.Headers.Where(pair => pair.Key == "Accept"
                || pair.Key.StartsWith("X-ms-", StringComparison.OrdinalIgnoreCase)).Select(pair => new {
                name = pair.Key,
                values = pair.Value.Select(v => pair.Key.Contains("Signature",
                    StringComparison.OrdinalIgnoreCase) ? "<synthetic-signature-omitted>" : v)
            }).ToArray();
            byte[] body = request.Content == null ? Array.Empty<byte>()
                : await request.Content.ReadAsByteArrayAsync(token);
            Console.WriteLine(JsonSerializer.Serialize(new {
                source = "local SDK formatter with terminal in-memory handler; no network",
                method = request.Method.Method, route = request.RequestUri!.AbsolutePath,
                headers, body = Convert.ToHexString(body)
            }));
            throw new InvalidOperationException("Local request experiment complete.");
        }
}
