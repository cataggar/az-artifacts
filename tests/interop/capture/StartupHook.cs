using System.Collections.Concurrent;
using System.Diagnostics;
using System.IO.Compression;
using System.Net;
using System.Net.Http.Headers;
using System.Security.Cryptography;
using System.Text.Json;
using System.Text.RegularExpressions;

// Reference-tool instrumentation only. No TLS interception or certificate changes.
public class StartupHook
{
    public static void Initialize()
    {
        string? path = Environment.GetEnvironmentVariable("AZ_ARTIFACTS_CAPTURE_PATH");
        if (path == null)
            throw new InvalidOperationException("A capture output path is required.");
        Capture.Initialize(path);
        DiagnosticListener.AllListeners.Subscribe(new ListenerObserver());
    }
}

internal static class Capture
{
    private static readonly object Gate = new();
    private static readonly ConcurrentDictionary<HttpRequestMessage, long> Requests = new();
    private static long _sequence;
    private static string _path = "";
    private static string? _packageName;
    private static readonly Regex Hash = new("^[0-9a-fA-F]{64}(01|02)?$", RegexOptions.Compiled);
    private static readonly Regex Word = new("^[a-zA-Z][a-zA-Z0-9_. -]{0,39}$", RegexOptions.Compiled);
    private const int Limit = 1024 * 1024;
    private const int ResponseLimit = 16 * Limit;

    public static void Initialize(string path)
    {
        _path = path;
        _packageName = Environment.GetEnvironmentVariable("AZ_ARTIFACTS_CAPTURE_PACKAGE_NAME");
        if (_packageName != null && !Regex.IsMatch(_packageName, "^[a-z0-9]+([._-][a-z0-9]+)*$"))
            throw new InvalidOperationException("A valid explicit capture package name is required.");
        Write(new { capture = "initialized", format = 2, tls = "unchanged" });
    }

    private static bool IsRegistration(string route) =>
        _packageName != null && route.Contains("/upack/packages/" + _packageName + "/");

    public static void Write(object value)
    {
        lock (Gate)
            File.AppendAllText(_path, JsonSerializer.Serialize(value) + "\n");
    }

    private static string Route(Uri? uri)
    {
        if (uri == null)
            return "<none>";
        // A signed storage path may itself contain a capability. Do not record it.
        if (!uri.Host.EndsWith(".visualstudio.com") && !uri.Host.EndsWith(".dev.azure.com")
            && uri.Host != "dev.azure.com" && !uri.IsLoopback)
            return "<storage-or-other>";
        return uri.AbsolutePath;
    }

    private static object Query(Uri? uri)
    {
        if (uri == null || Route(uri) == "<storage-or-other>")
            return Array.Empty<object>();
        return uri.Query.TrimStart('?').Split('&', StringSplitOptions.RemoveEmptyEntries)
            .Select(part => {
                string[] pair = part.Split('=', 2);
                string key = Uri.UnescapeDataString(pair[0]);
                string value = pair.Length == 2 ? Uri.UnescapeDataString(pair[1]) : "";
                return new {
                    name = key,
                    value = key.ToLowerInvariant() switch {
                        "api-version" or "keepuntil" or "compression" or "allowedge"
                        or "intent" or "domainid" or "includemissing" => SafeString(value),
                        _ => "<redacted>"
                    }
                };
            }).ToArray();
    }

    private static object Headers(HttpHeaders headers) =>
        headers.Where(pair => !Regex.IsMatch(pair.Key,
            "authorization|cookie|token|secret|signature", RegexOptions.IgnoreCase))
        .Select(pair => new {
            name = pair.Key,
            values = pair.Key.ToLowerInvariant() switch {
                "content-type" or "content-length" or "content-encoding" or "content-range"
                or "accept" or "range" or "retry-after" => pair.Value.Cast<object>().ToArray(),
                "x-ms-keepuntils" => pair.Value.Select(v => StructuralHeader(v, false)).ToArray(),
                _ when Regex.IsMatch(pair.Key, "^x-ms-chunk-[0-9a-f]{64}(01|02)$",
                    RegexOptions.IgnoreCase) =>
                    pair.Value.Select(v => StructuralHeader(v, true)).ToArray(),
                _ => new object[] { "<redacted>" }
            }
        }).ToArray();

    private static object StructuralHeader(string value, bool chunk)
    {
        if (chunk && Regex.IsMatch(value, @"^[0-9]{1,10}/(true|false)$")) {
            string[] fields = value.Split('/');
            return new { encoding = "chunk-length/compressed",
                length = long.Parse(fields[0]), compressed = fields[1] == "true" };
        }
        if (!chunk && value.Length <= 512 * 21) {
            string[] dates = value.Split(',');
            if (dates.Length <= 512 && dates.All(date => Regex.IsMatch(date,
                @"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")))
                return new { encoding = "utc-date-list", values = dates };
        }
        if (chunk && Regex.IsMatch(value, @"^[0-9,;:. +\-]{1,256}$"))
            return new { encoding = "numeric-text", value };
        if (value.Length > Limit)
            return new { encoding = "omitted", length = value.Length };
        byte[] data = System.Text.Encoding.UTF8.GetBytes(value);
        string encoding = "json";
        try {
            using var direct = JsonDocument.Parse(data);
            return new { encoding, structure = ReceiptShape(direct.RootElement) };
        } catch (JsonException) { }
        try {
            data = Convert.FromBase64String(value);
            encoding = "base64";
        } catch (FormatException) {
            return new { encoding = "opaque-text", length = value.Length };
        }
        if (data.Length >= 2 && data[0] == 31 && data[1] == 139) {
            using var input = new MemoryStream(data);
            using var gzip = new GZipStream(input, CompressionMode.Decompress);
            using var output = new MemoryStream();
            byte[] buffer = new byte[4096];
            int read;
            while ((read = gzip.Read(buffer)) != 0) {
                if (output.Length + read > Limit)
                    return new { encoding = "base64-gzip", omitted = "decoded size limit" };
                output.Write(buffer, 0, read);
            }
            data = output.ToArray();
            encoding += "-gzip";
        }
        try {
            using var document = JsonDocument.Parse(data);
            return new { encoding = encoding + "-json",
                structure = ReceiptShape(document.RootElement) };
        } catch (JsonException) {
            return new { encoding, decoded_length = data.Length, value = "<opaque>" };
        }
    }

    private static object? ReceiptShape(JsonElement value, string key = "")
    {
        string[] fields = { "id", "key", "value", "blobid", "dedupid", "keepuntil",
            "expiration", "expires", "signature", "receipt", "receipts", "version",
            "offset", "length", "compressedlength", "uncompressedlength", "compression" };
        return value.ValueKind switch {
            JsonValueKind.Object => value.EnumerateObject().Select(p => new {
                field = Hash.IsMatch(p.Name) || fields.Contains(p.Name.ToLowerInvariant())
                    ? p.Name : "<unknown-field>",
                value = ReceiptShape(p.Value, p.Name)
            }).ToArray(),
            JsonValueKind.Array => value.EnumerateArray().Select(v => ReceiptShape(v, key)).ToArray(),
            JsonValueKind.String when key.Equals("keepUntil", StringComparison.OrdinalIgnoreCase)
                && DateTimeOffset.TryParse(value.GetString(), out _) => value.GetString(),
            JsonValueKind.String when (key.Equals("blobId", StringComparison.OrdinalIgnoreCase)
                || key.Equals("dedupId", StringComparison.OrdinalIgnoreCase))
                && Hash.IsMatch(value.GetString()!) => value.GetString(),
            JsonValueKind.String => new { kind = "opaque-string", length = value.GetString()!.Length },
            JsonValueKind.Number when new[] { "version", "offset", "length", "compressedlength",
                "uncompressedlength", "compression" }.Contains(key.ToLowerInvariant()) => value.Clone(),
            JsonValueKind.Number => new { kind = "number" },
            JsonValueKind.True or JsonValueKind.False => new { kind = "boolean" },
            _ => null
        };
    }

    private static object SafeString(string value)
    {
        if (Hash.IsMatch(value) || Guid.TryParse(value, out _) || Word.IsMatch(value)
            || DateTimeOffset.TryParse(value, out _)
            || Regex.IsMatch(value, @"^\d+(\.\d+)*(-preview(\.\d+)?)?$"))
            return value;
        return new { kind = "string", length = value.Length, value = "<redacted>" };
    }

    private static object? Sanitize(JsonElement value, string key = "")
    {
        if (Regex.IsMatch(key, "token|secret|credential|authorization|signature|password",
            RegexOptions.IgnoreCase))
            return "<redacted>";
        if (Regex.IsMatch(key, "receipt|capability|keepuntils", RegexOptions.IgnoreCase))
            return ReceiptShape(value);
        return value.ValueKind switch {
            JsonValueKind.Object => value.EnumerateObject().ToDictionary(p => p.Name,
                p => Sanitize(p.Value, p.Name)),
            JsonValueKind.Array => value.EnumerateArray().Select(v => Sanitize(v, key)).ToArray(),
            JsonValueKind.String => key == "proofNodes" ? SyntheticProof(value.GetString()!)
                : SafeString(value.GetString()!),
            JsonValueKind.Number => value.Clone(),
            JsonValueKind.True => true,
            JsonValueKind.False => false,
            _ => null
        };
    }

    private static object SyntheticProof(string value)
    {
        // Only allow a proof when it is a hexadecimal node or base64 node bytes.
        if (Hash.IsMatch(value))
            return value;
        try {
            byte[] decoded = Convert.FromBase64String(value);
            if (decoded.Length >= 4 && decoded.Length <= 20484 && decoded[0] == 0
                && decoded[1] == 0)
                return new { encoding = "base64", value };
        } catch (FormatException) { }
        return new { kind = "proof", length = value.Length, value = "<redacted>" };
    }

    private static object? Body(HttpContent? content, string route, bool request)
    {
        if (content == null)
            return null;
        bool dedup = route.Contains("/dedup/", StringComparison.OrdinalIgnoreCase);
        bool registration = IsRegistration(route);
        bool local = route.StartsWith("/capture-test");
        if (!dedup && !registration && !local)
            return new { omitted = "outside synthetic package protocol" };
        if (content.Headers.ContentLength > ResponseLimit)
            return new { omitted = "size limit" };
        content.LoadIntoBufferAsync(ResponseLimit).GetAwaiter().GetResult();
        byte[] data = content.ReadAsByteArrayAsync().GetAwaiter().GetResult();
        if (data.Length > ResponseLimit)
            throw new InvalidOperationException("Capture body exceeds the configured limit.");
        string sha256 = Convert.ToHexString(SHA256.HashData(data)).ToLowerInvariant();
        byte[] decoded = data;
        if (content.Headers.ContentEncoding.Contains("gzip")) {
            using var compressed = new MemoryStream(data);
            using var gzip = new GZipStream(compressed, CompressionMode.Decompress);
            using var output = new MemoryStream();
            byte[] buffer = new byte[4096];
            int read;
            while ((read = gzip.Read(buffer)) != 0) {
                if (output.Length + read > ResponseLimit)
                    throw new InvalidOperationException("Decoded capture exceeds the size limit.");
                output.Write(buffer, 0, read);
            }
            decoded = output.ToArray();
        }
        if (data.Length == 0)
            return new { length = 0, sha256 };
        try {
            using var json = JsonDocument.Parse(decoded);
            return new { length = data.Length, sha256, json = Sanitize(json.RootElement) };
        } catch (JsonException) {
            // Binary upload bodies contain only the approved synthetic file or dedup nodes.
            if (request && (dedup || local))
                return new { length = data.Length, sha256, base64 = Convert.ToBase64String(data) };
            return new { length = data.Length, sha256, omitted = "non-JSON response" };
        }
    }

    public static void StreamedBody(long sequence, string route, BodyTeeStream body, bool complete)
    {
        object summary;
        if (body.TotalBytes > body.Captured.Length) {
            summary = new { length = body.TotalBytes, sha256 = body.Hash,
                omitted = "streamed synthetic payload; raw bytes not retained" };
        } else {
            byte[] data = body.Captured.ToArray();
            try {
                using var json = JsonDocument.Parse(data);
                summary = new { length = body.TotalBytes, sha256 = body.Hash,
                    json = Sanitize(json.RootElement) };
            } catch (JsonException) {
                summary = new { length = body.TotalBytes, sha256 = body.Hash,
                    base64 = Convert.ToBase64String(data) };
            }
        }
        Write(new { phase = "request_body", sequence, route, complete, body = summary });
    }

    public static void Event(string name, object? value)
    {
        if (value == null)
            return;
        var type = value.GetType();
        var request = type.GetProperty("Request")?.GetValue(value) as HttpRequestMessage;
        if (request == null)
            return;
        string route = Route(request.RequestUri);
        if (name.EndsWith(".Start")) {
            long sequence = Interlocked.Increment(ref _sequence);
            Requests[request] = sequence;
            bool protocol = route.Contains("/dedup/") || IsRegistration(route)
                || route.StartsWith("/capture-test");
            if (protocol && request.Content != null) {
                int limit = route.Contains("/dedup/chunks") || route.StartsWith("/capture-test")
                    ? 65536 : Limit;
                request.Content = new CapturedContent(request.Content, sequence, route, limit);
            }
            Write(new {
                phase = "request", sequence, method = request.Method.Method, route,
                query = Query(request.RequestUri),
                headers = Headers(request.Headers),
                content_headers = request.Content == null ? null : Headers(request.Content.Headers),
                body = request.Content == null ? null
                    : new { capture = protocol ? "streamed" : "omitted" }
            });
        } else if (name.EndsWith(".Stop")) {
            var response = type.GetProperty("Response")?.GetValue(value) as HttpResponseMessage;
            Requests.TryRemove(request, out long sequence);
            Write(new {
                phase = "response", sequence, route,
                status = response == null ? (int?)null : (int)response.StatusCode,
                headers = response == null ? null : Headers(response.Headers),
                content_headers = response?.Content == null ? null : Headers(response.Content.Headers),
                body = Body(response?.Content, route, false)
            });
        }
    }
}

internal sealed class CapturedContent : HttpContent
{
    private readonly HttpContent _inner;
    private readonly long _sequence;
    private readonly string _route;
    private readonly int _limit;

    public CapturedContent(HttpContent inner, long sequence, string route, int limit)
    {
        _inner = inner;
        _sequence = sequence;
        _route = route;
        _limit = limit;
        _ = inner.Headers.ContentLength;
        foreach (var header in inner.Headers)
            Headers.TryAddWithoutValidation(header.Key, header.Value);
    }

    protected override bool TryComputeLength(out long length)
    {
        length = _inner.Headers.ContentLength ?? 0;
        return _inner.Headers.ContentLength.HasValue;
    }

    protected override Task SerializeToStreamAsync(Stream stream, TransportContext? context) =>
        SendAsync(stream, context, CancellationToken.None);

    protected override Task SerializeToStreamAsync(Stream stream, TransportContext? context,
        CancellationToken cancellationToken) => SendAsync(stream, context, cancellationToken);

    private async Task SendAsync(Stream stream, TransportContext? context, CancellationToken token)
    {
        using var tee = new BodyTeeStream(stream, _limit);
        bool complete = false;
        try {
            await _inner.CopyToAsync(tee, context, token).ConfigureAwait(false);
            complete = true;
        } finally {
            Capture.StreamedBody(_sequence, _route, tee, complete);
        }
    }

    protected override void Dispose(bool disposing)
    {
        if (disposing)
            _inner.Dispose();
        base.Dispose(disposing);
    }
}

internal sealed class BodyTeeStream(Stream destination, int limit) : Stream
{
    private readonly IncrementalHash _hash = IncrementalHash.CreateHash(HashAlgorithmName.SHA256);
    public MemoryStream Captured { get; } = new();
    public long TotalBytes { get; private set; }
    public string Hash => Convert.ToHexString(_hash.GetCurrentHash()).ToLowerInvariant();

    private void Record(ReadOnlySpan<byte> buffer)
    {
        _hash.AppendData(buffer);
        TotalBytes += buffer.Length;
        int count = (int)Math.Min(buffer.Length, limit - Captured.Length);
        if (count > 0)
            Captured.Write(buffer[..count]);
    }

    public override void Write(byte[] buffer, int offset, int count) =>
        Write(buffer.AsSpan(offset, count));
    public override void Write(ReadOnlySpan<byte> buffer)
    {
        destination.Write(buffer);
        Record(buffer);
    }
    public override async ValueTask WriteAsync(ReadOnlyMemory<byte> buffer,
        CancellationToken cancellationToken = default)
    {
        await destination.WriteAsync(buffer, cancellationToken).ConfigureAwait(false);
        Record(buffer.Span);
    }
    public override Task WriteAsync(byte[] buffer, int offset, int count,
        CancellationToken cancellationToken) =>
        WriteAsync(buffer.AsMemory(offset, count), cancellationToken).AsTask();
    public override void Flush() => destination.Flush();
    public override Task FlushAsync(CancellationToken token) => destination.FlushAsync(token);
    public override bool CanRead => false;
    public override bool CanSeek => false;
    public override bool CanWrite => true;
    public override long Length => throw new NotSupportedException();
    public override long Position { get => throw new NotSupportedException();
        set => throw new NotSupportedException(); }
    public override int Read(byte[] buffer, int offset, int count) => throw new NotSupportedException();
    public override long Seek(long offset, SeekOrigin origin) => throw new NotSupportedException();
    public override void SetLength(long value) => throw new NotSupportedException();
    protected override void Dispose(bool disposing)
    {
        if (disposing) {
            Captured.Dispose();
            _hash.Dispose();
        }
        base.Dispose(disposing);
    }
}

internal sealed class ListenerObserver : IObserver<DiagnosticListener>
{
    public void OnNext(DiagnosticListener listener)
    {
        if (listener.Name == "HttpHandlerDiagnosticListener")
            listener.Subscribe(new EventObserver(), name =>
                name == "System.Net.Http.HttpRequestOut"
                || name == "System.Net.Http.HttpRequestOut.Start"
                || name == "System.Net.Http.HttpRequestOut.Stop");
    }
    public void OnCompleted() { }
    public void OnError(Exception error) { Environment.Exit(90); }
}

internal sealed class EventObserver : IObserver<KeyValuePair<string, object?>>
{
    public void OnNext(KeyValuePair<string, object?> value)
    {
        try { Capture.Event(value.Key, value.Value); }
        catch { Environment.Exit(90); }
    }
    public void OnCompleted() { }
    public void OnError(Exception error) { Environment.Exit(90); }
}
