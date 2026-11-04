"""Time build and replay on a synthetic recording of 5,000 requests. Best of 5, so numbers reproduce on a busy machine."""
import json, pathlib, sys, threading, time, urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import har2clone as h


def recording(n=5000):
    body = json.dumps({"id": 1, "name": "x", "tags": ["a", "b"], "owner": {"id": 2, "email": "a@b.test"}})
    entries = [{"request": {"method": "GET", "url": f"https://a.test/api/items/{i % 500}/parts/{i}?page={i % 7}", "headers": []},
                "response": {"status": 200, "headers": [{"name": "Set-Cookie", "value": "a=b"}], "content": {"mimeType": "application/json", "text": body}}}
               for i in range(n)]
    return {"log": {"entries": entries}}


har = recording()
builds = []
for _ in range(5):
    t = time.perf_counter()
    h.build(har)
    builds.append((time.perf_counter() - t) * 1000)
s = h.build(har)
srv = h.make_server(s, 0)
threading.Thread(target=srv.serve_forever, daemon=True).start()
url = f"http://127.0.0.1:{srv.server_address[1]}/api/items/3/parts/99"
runs = []
for _ in range(5):
    t = time.perf_counter()
    for _ in range(200):
        urllib.request.urlopen(url, timeout=5).read()
    runs.append((time.perf_counter() - t) / 200 * 1000)
srv.shutdown()
print(f"build 5,000 requests: {min(builds):.0f} ms")
print(f"replay one HTTP answer: {min(runs):.2f} ms")
