"""Write examples/shop.har: a synthetic recording of a small coffee shop app. Every value in it is fake."""
import base64, json, pathlib

TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJkZW1vIn0.ZmFrZS1zaWduYXR1cmUtZm9yLWRlbW8"


def entry(method, url, status, body, page="p1", mime="application/json", req=None, kind="xhr", encoding=None):
    e = {"pageref": page, "_resourceType": kind,
         "request": {"method": method, "url": url, "headers": [{"name": "Authorization", "value": "Bearer " + TOKEN}]},
         "response": {"status": status,
                      "headers": [{"name": "Set-Cookie", "value": "sid=fake-session"}, {"name": "X-Total-Count", "value": "3"},
                                  {"name": "Strict-Transport-Security", "value": "max-age=1"}],
                      "content": {"mimeType": mime, "text": body}}}
    if encoding:
        e["response"]["content"]["encoding"] = encoding
    if req is not None:
        e["request"]["postData"] = {"mimeType": "application/json", "text": json.dumps(req)}
    return e


def j(v):
    return json.dumps(v, ensure_ascii=False)


A = "https://api.brewline.example"
beans = [{"id": i, "name": n, "price": p, "roast": r} for i, n, p, r in
         [(1, "Yirgacheffe", 18.5, "light"), (2, "Huila", 16.0, "medium"), (3, "Sumatra Mandheling", 17.25, "dark")]]
entries = [
    entry("GET", "https://brewline.example/", 200, "<html><title>Brewline</title></html>", mime="text/html", kind="document"),
    entry("GET", "https://brewline.example/app.js", 200, "this.token=t;fetch('/api/v1/beans')", mime="text/javascript", kind="script"),
    entry("GET", A + "/api/v1/beans?page=1&limit=3", 200, j({"items": beans, "next": None})),
    entry("GET", A + "/api/v1/beans/1", 200, j(beans[0])),
    entry("GET", A + "/api/v1/beans/2", 200, j(beans[1])),
    entry("GET", A + "/api/v1/beans/3", 200, j(beans[2])),
    entry("GET", A + "/api/v1/beans/2/reviews", 200, j([{"id": 91, "stars": 5, "text": "Caramel, très doux ☕"}])),
    entry("GET", A + "/api/v1/beans/3/reviews", 200, j([{"id": 92, "stars": 4, "text": "Earthy"}])),
    entry("POST", A + "/api/v1/auth/login", 200, j({"access_token": TOKEN, "user": {"id": 7, "name": "Sam"}}),
          page="p2", req={"email": "sam@example.test", "password": "hunter2-fake"}),
    entry("GET", A + "/api/v1/cart?session=abc123&currency=EUR", 200, j({"lines": [], "total": 0}), page="p2"),
    entry("POST", A + "/api/v1/cart/items", 201, j({"lines": [{"bean": 2, "qty": 1}], "total": 16.0}), page="p2", req={"bean": 2, "qty": 1}),
    entry("GET", A + "/api/v1/orders/poll", 200, j({"state": "queued"}), page="p2"),
    entry("GET", A + "/api/v1/orders/poll", 200, j({"state": "roasting"}), page="p2"),
    entry("POST", A + "/api/v1/checkout", 402, j({"error": "card_declined"}), page="p2", req={"card": "fake"}),
    entry("POST", A + "/graphql", 200, j({"data": {"me": {"id": 7}}}), page="p2", req={"operationName": "GetMe", "query": "query GetMe { me { id } }"}),
    entry("POST", A + "/graphql", 200, j({"data": {"stock": 12}}), page="p2", req={"operationName": "GetStock", "query": "query GetStock { stock }"}),
    entry("GET", A + "/api/v1/receipts/ab12cd34ef56ab78", 200, base64.b64encode(b"%PDF-fake").decode(), page="p2", mime="application/pdf", encoding="base64"),
    entry("GET", A + "/api/v1/loyalty", 404, j({"error": "not found"}), page="p2"),
]
har = {"log": {"version": "1.2", "creator": {"name": "make_shop_har.py", "version": "1"},
               "pages": [{"id": "p1", "title": "Browse the shop"}, {"id": "p2", "title": "Sign in and check out"}],
               "entries": entries}}
pathlib.Path(__file__).with_name("shop.har").write_text(json.dumps(har, ensure_ascii=False, indent=1), encoding="utf-8")
