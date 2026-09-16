using System.IO.Compression;
using System.Net;
using System.Net.Sockets;
using System.Security.Cryptography;
using System.Text;

byte[] Compress(byte[] value)
{
    using var output = new MemoryStream();
    using (var gzip = new GZipStream(output, CompressionMode.Compress, leaveOpen: true))
        gzip.Write(value);
    return output.ToArray();
}

const string receipt = "{\"blobId\":\"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA01\","
    + "\"keepUntil\":\"2000-01-03T19:10:51Z\",\"receipt\":\"opaque-receipt-canary\","
    + "\"signature\":\"signed-receipt-canary\",\"value\":\"short-secret\",\"version\":1}";
var listener = new TcpListener(IPAddress.Loopback, 0);
listener.Start();
int port = ((IPEndPoint)listener.LocalEndpoint).Port;
string? receivedHash = null;
long receivedBytes = 0;
Task server = Task.Run(async () => {
    using TcpClient connection = await listener.AcceptTcpClientAsync();
    using NetworkStream stream = connection.GetStream();
    using var header = new MemoryStream();
    byte[] buffer = new byte[65536];
    while (header.Length < 65536) {
        int read = await stream.ReadAsync(buffer.AsMemory(0, 1));
        if (read == 0)
            throw new InvalidOperationException("Missing self-test request headers.");
        header.WriteByte(buffer[0]);
        if (header.Length >= 4 && Encoding.ASCII.GetString(header.GetBuffer(),
            (int)header.Length - 4, 4) == "\r\n\r\n")
            break;
    }
    string headers = Encoding.ASCII.GetString(header.ToArray());
    string lengthHeader = headers.Split("\r\n").Single(
        line => line.StartsWith("Content-Length:", StringComparison.OrdinalIgnoreCase));
    long length = long.Parse(lengthHeader.Split(':', 2)[1].Trim());
    using var digest = IncrementalHash.CreateHash(HashAlgorithmName.SHA256);
    while (receivedBytes < length) {
        int read = await stream.ReadAsync(buffer.AsMemory(0,
            (int)Math.Min(buffer.Length, length - receivedBytes)));
        if (read == 0)
            throw new InvalidOperationException("Truncated self-test request.");
        digest.AppendData(buffer.AsSpan(0, read));
        receivedBytes += read;
    }
    receivedHash = Convert.ToHexString(digest.GetHashAndReset()).ToLowerInvariant();
    byte[] raw = Encoding.UTF8.GetBytes(
        "{\"status\":\"NeedAction\",\"missing\":3,\"token\":\"never-record-this\","
        + "\"url\":\"https://blob.example/file?sig=never-record-this\","
        + "\"Receipts\":{\"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA01\":"
        + receipt + "}}");
    byte[] body = Compress(raw);
    byte[] responseHeaders = Encoding.ASCII.GetBytes(
        "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
        + $"Content-Length: {body.Length}\r\nContent-Encoding: gzip\r\nConnection: close\r\n\r\n");
    await stream.WriteAsync(responseHeaders);
    await stream.WriteAsync(body);
});
using var http = new HttpClient(new HttpClientHandler {
    AutomaticDecompression = DecompressionMethods.GZip
});
using var request = new HttpRequestMessage(HttpMethod.Post,
    $"http://127.0.0.1:{port}/capture-test?api-version=1.0-preview.1&sig=never-record-this");
request.Headers.Authorization =
    new System.Net.Http.Headers.AuthenticationHeaderValue("Bearer", "never-record-this");
request.Headers.TryAddWithoutValidation(
    "X-ms-chunk-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA01", "0,44,44,0");
request.Headers.TryAddWithoutValidation(
    "X-ms-chunk-BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB01", "113739/false");
request.Headers.TryAddWithoutValidation("X-MS-KeepUntils", new[] {
    receipt, Convert.ToBase64String(Encoding.UTF8.GetBytes(receipt)),
    Convert.ToBase64String(Compress(Encoding.UTF8.GetBytes(receipt))),
    "2030-01-02T03:04:05Z,2030-01-03T03:04:05Z"
});
using Stream payload = args.Length == 1 ? File.OpenRead(args[0])
    : new MemoryStream(Encoding.UTF8.GetBytes("synthetic payload"));
string expectedHash = Convert.ToHexString(SHA256.HashData(payload)).ToLowerInvariant();
payload.Position = 0;
long expectedLength = payload.Length;
request.Content = new StreamContent(payload, 65536);
using var response = await http.SendAsync(request);
string result = await response.Content.ReadAsStringAsync();
await server;
listener.Stop();
if (!result.Contains("NeedAction") || response.StatusCode != HttpStatusCode.OK
    || receivedHash != expectedHash || receivedBytes != expectedLength)
    throw new InvalidOperationException("Self-test failed.");
Console.WriteLine(System.Text.Json.JsonSerializer.Serialize(new {
    self_test = "passed", bytes = receivedBytes, sha256 = receivedHash
}));
