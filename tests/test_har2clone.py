import base64, json, os, pathlib, subprocess, sys, tempfile, threading, unittest, urllib.error, urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import har2clone as h

SHOP = ROOT / "examples" / "shop.har"
GH = "ghp_" + "aBcD1e2F3g4H5i6J7k8L9m0N1o2P3q4R5s6T7u8V"  # assembled so no token-shaped text sits in the repo
JWT = "eyJhbGciOiJub25lIn0" + ".eyJzdWIiOiIxIn0." + "c2lnbmF0dXJlLWZha2U"


def har(*entries):
    return {"log": {"entries": list(entries)}}


def ent(method, url, status=200, body="{}", mime="application/json", headers=(), post=None, kind=None):
    e = {"request": {"method": method, "url": url, "headers": []},
         "response": {"status": status, "headers": [{"name": k, "value": v} for k, v in headers],
                      "content": {"mimeType": mime, "text": body}}}
    if post is not None:
        e["request"]["postData"] = {"mimeType": "application/json", "text": post}
    if kind:
        e["_resourceType"] = kind
    return e


def build(*entries, **kw):
    return h.build(har(*entries), **kw)


def templates(s):
    return [(r["method"], r["template"]) for r in s["routes"]]


class Secrets(unittest.TestCase):
    def test_credential_headers_cookies_and_tokens_never_reach_the_file(self):
        s = build(ent("GET", f"https://a.test/me?access_token={GH}&page=2", body=json.dumps(
            {"token": "plain-secret-value", "password": "pw-value", "note": f"ref={GH}", "url": "https://u:pw@h.test/?sig=abc"}),
            headers=[("Set-Cookie", "sid=cookie-value"), ("X-Api-Key", "header-key-value"), ("X-Total-Count", "9")]))
        text = json.dumps(s)
        for leak in (GH, "plain-secret-value", "pw-value", "cookie-value", "header-key-value", "u:pw@", "sig=abc"):
            self.assertNotIn(leak, text)
        sample = s["routes"][0]["samples"][0]
        self.assertEqual(sample["query"], "access_token=%5Bdropped%5D&page=2")
        self.assertIn(("X-Total-Count", "9"), [tuple(x) for x in sample["headers"]])
        self.assertGreaterEqual(s["dropped"]["headers"], 2)
        self.assertGreaterEqual(s["dropped"]["json_values"], 2)

    def test_headers_ending_in_key_are_dropped(self):
        s = build(ent("GET", "https://a.test/x", headers=[("X-Functions-Key", "SECRET_H1"), ("Ocp-Apim-Subscription-Key", "SECRET_H2")]))
        text = json.dumps(s)
        self.assertNotIn("SECRET_H1", text)
        self.assertNotIn("SECRET_H2", text)
        self.assertEqual(s["dropped"]["headers"], 2)

    def test_user_info_in_a_url_reaches_no_output_and_is_counted(self):
        s = build(ent("GET", "https://SECRET_USER:SECRET_PASS@api.x.test:8443/ui"), ent("GET", "https://api.x.test:8443/ok"),
                  only_hosts=["api.x.test:8443"])
        self.assertEqual(s["hosts"], ["api.x.test:8443"])
        self.assertEqual(len(s["routes"]), 2)
        self.assertEqual(s["dropped"]["url_userinfo"], 1)
        with tempfile.TemporaryDirectory() as d:
            h.write_outputs(s, d)
            everything = json.dumps(s) + "".join(p.read_text(encoding="utf-8") for p in pathlib.Path(d).iterdir())
        self.assertNotIn("SECRET_USER", everything)
        self.assertNotIn("SECRET_PASS", everything)

    def test_what_must_survive_survives(self):
        body = json.dumps({"plan": "Basic Membership", "key": "enter", "code": "EUR", "session": "morning", "task": "task-management"})
        s = build(ent("GET", "https://a.test/plans", body=body))
        self.assertEqual(s["routes"][0]["samples"][0]["text"], body)

    def test_scripts_keep_code_but_lose_real_tokens(self):
        js = f"this.token=t;var a='{GH}';"
        s = build(ent("GET", "https://a.test/app.js", body=js, mime="text/javascript"), include_static=True)
        text = s["routes"][0]["samples"][0]["text"]
        self.assertIn("this.token=t", text)
        self.assertNotIn(GH, text)

    def test_hidden_value_goes_everywhere_in_every_spelling(self):
        s = build(ent("GET", "https://a.test/u/sam%40x.test?who=sam%40x.test", body='{"e":"sam@x.test","f":"c2FtQHgudGVzdA=="}'),
                  hidden=["sam@x.test"])
        text = json.dumps(s)
        self.assertNotIn("sam@x.test", text)
        self.assertNotIn("sam%40x.test", text)
        self.assertNotIn("c2FtQHgudGVzdA", text)

    def test_binary_body_holding_a_secret_is_left_out(self):
        e = ent("GET", "https://a.test/f", body=base64.b64encode(GH.encode()).decode(), mime="application/octet-stream")
        e["response"]["content"]["encoding"] = "base64"
        sample = build(e)["routes"][0]["samples"][0]
        self.assertEqual(sample["body_left_out"], "held_secret")
        self.assertNotIn("base64", sample)

    def test_secret_in_path_becomes_id(self):
        s = build(ent("GET", f"https://a.test/share/{JWT}/view"))
        self.assertEqual(templates(s), [("GET", "/share/{id}/view")])
        self.assertNotIn(JWT, json.dumps(s))


class Templates(unittest.TestCase):
    def test_ids_by_form(self):
        for good in ("12", "550e8400-e29b-41d4-a716-446655440000", "ab12cd34ef56ab78", "a1b2c3d4e5f6g7h8i9j0k"):
            self.assertTrue(h.is_id_segment(good), good)
        for bad in ("users", "v2", "facade", "report.pdf", "abc123"):
            self.assertFalse(h.is_id_segment(bad), bad)

    def test_word_ids_merge_only_with_evidence(self):
        s = build(*[ent("GET", f"https://a.test/users/{n}/posts") for n in ("sam", "pat")],
                  *[ent("GET", f"https://a.test/teams/{n}", body='{"id":1}') for n in ("red", "blue", "green")])
        self.assertEqual(sorted(templates(s)), [("GET", "/teams/{id}"), ("GET", "/users/{id}/posts")])

    def test_namespaces_and_dotted_names_stay_apart(self):
        s = build(*[ent("GET", f"https://a.test/api/{n}", body='{"a":1}') for n in ("users", "posts", "tags")],
                  *[ent("GET", f"https://a.test/f/{n}.png", body='{"a":1}') for n in "abc"])
        self.assertEqual(len(s["routes"]), 6)


class Replay(unittest.TestCase):
    def setUp(self):
        self.s = h.build(json.loads(SHOP.read_text(encoding="utf-8")))
        self.r = h.Replayer(self.s)

    def ask(self, method, path, query="", body=b""):
        return self.r.answer(method, path, query, body)

    def test_exact_then_template(self):
        self.assertEqual(json.loads(self.ask("GET", "/api/v1/beans/2")["body"])["name"], "Huila")
        self.assertEqual(self.ask("GET", "/api/v1/beans/777")["how"], "template")

    def test_more_literal_template_wins(self):
        s = build(ent("GET", "https://a.test/users/1", body='{"a":1}'), ent("GET", "https://a.test/users/search", body='{"b":1}'))
        self.assertEqual(json.loads(h.Replayer(s).answer("GET", "/users/search", "", b"")["body"]), {"b": 1})

    def test_repeats_cycle_in_recording_order(self):
        seen = [json.loads(self.ask("GET", "/api/v1/orders/poll")["body"])["state"] for _ in range(3)]
        self.assertEqual(seen, ["queued", "roasting", "queued"])

    def test_graphql_operations_stay_apart(self):
        ask = lambda op: self.ask("POST", "/graphql", "", json.dumps({"operationName": op}).encode())
        self.assertEqual(json.loads(ask("GetStock")["body"])["data"], {"stock": 12})
        self.assertEqual(json.loads(ask("GetMe")["body"])["data"], {"me": {"id": 7}})
        self.assertEqual(ask("Unknown")["miss"], "operation_not_recorded")
        q = {"query": "query GetStock { stock }"}
        self.assertEqual(json.loads(self.ask("POST", "/graphql", "", json.dumps(q).encode())["body"])["data"], {"stock": 12})

    def test_recorded_error_status_is_replayed(self):
        self.assertEqual(self.ask("POST", "/api/v1/checkout")["status"], 402)
        self.assertEqual(self.ask("GET", "/api/v1/loyalty")["status"], 404)

    def test_misses_say_why_and_suggest(self):
        self.assertEqual(self.ask("DELETE", "/api/v1/beans/2")["miss"], "method_not_recorded")
        m = self.ask("GET", "/api/v1/bean")
        self.assertEqual(m["miss"], "path_not_recorded")
        self.assertTrue(m["nearest"])
        self.assertEqual(len(self.r.misses), 2)

    def test_writes_do_not_change_reads(self):
        before = self.ask("GET", "/api/v1/cart", "session=x&currency=EUR")["body"]
        self.ask("POST", "/api/v1/cart/items", "", b'{"bean":2,"qty":1}')
        self.assertEqual(self.ask("GET", "/api/v1/cart", "session=x&currency=EUR")["body"], before)

    def test_binary_round_trip(self):
        self.assertEqual(self.ask("GET", "/api/v1/receipts/ab12cd34ef56ab78")["body"], b"%PDF-fake")

    def test_304_uses_cached_body_or_says_it_cannot(self):
        s = build(ent("GET", "https://a.test/a", 304, '{"ok":1}'), ent("GET", "https://a.test/b", 304, ""))
        r = h.Replayer(s)
        self.assertEqual(r.answer("GET", "/a", "", b"")["status"], 200)
        self.assertEqual(r.answer("GET", "/b", "", b"")["miss"], "cached_body_unavailable")
        self.assertEqual(s["skipped"]["uncached_304"], 1)


class UpstreamRecording(unittest.TestCase):
    """The hand-written recording and expected answers from Craft Anything's stand-in test."""

    def setUp(self):
        def e(method, path, status, body):
            x = ent(method, "https://api.example.test" + path, status, body, headers=[("Set-Cookie", "sid=sk-test-cookie"), ("X-Total-Count", "2")])
            x["request"]["headers"] = [{"name": "Authorization", "value": "Bearer sk-test"}]
            return x
        self.s = build(e("GET", "/users/1", 200, '{"id":1,"name":"Pat"}'), e("GET", "/users/2", 200, '{"id":2,"name":"Sam"}'),
                       e("GET", "/poll", 200, '{"n":1}'), e("GET", "/poll", 200, '{"n":2}'),
                       e("POST", "/users", 201, '{"id":3}'), e("GET", "/gone", 404, '{"recorded":"not found"}'))
        self.r = h.Replayer(self.s)

    def test_same_routes_and_no_secret(self):
        self.assertEqual(len(self.s["routes"]), 4)
        self.assertIn(("GET", "/users/{id}"), templates(self.s))
        self.assertNotIn("sk-test", json.dumps(self.s))

    def test_same_answers(self):
        get = lambda p: self.r.answer("GET", p, "", b"")
        self.assertIn(b"Pat", get("/users/1")["body"])
        self.assertIn(b"Pat", get("/users/77")["body"])
        self.assertEqual([get("/poll")["body"] for _ in range(3)], [b'{"n":1}', b'{"n":2}', b'{"n":1}'])
        self.assertEqual(get("/gone")["status"], 404)
        post = self.r.answer("POST", "/users", "", b"{}")
        self.assertEqual((post["status"], post["body"]), (201, b'{"id":3}'))
        self.assertIn(b"Sam", get("/users/2")["body"])

    def test_same_no_match(self):
        m = self.r.answer("GET", "/users/1/nope", "", b"")
        self.assertEqual(m["miss"], "path_not_recorded")
        self.assertEqual(m["nearest"][0], "GET /users/{id}")


class Skips(unittest.TestCase):
    def test_static_data_and_failed_entries_are_counted_not_kept(self):
        s = build(ent("GET", "https://a.test/s.css", body="a{}", mime="text/css"), ent("GET", "data:text/plain,x"),
                  ent("GET", "https://a.test/blocked", 0, ""), ent("GET", "https://a.test/page", body="<p>", kind="document"))
        self.assertEqual(s["routes"], [])
        self.assertEqual(s["skipped"], {"static": 2, "not_http": 1, "no_response": 1, "uncached_304": 0, "other_host": 0})

    def test_only_hosts(self):
        s = build(ent("GET", "https://a.test/x"), ent("GET", "https://b.test/y"), only_hosts=["b.test"])
        self.assertEqual(templates(s), [("GET", "/y")])

    def test_broken_file_is_a_clear_error(self):
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d, "x.har")
            for text in ("not json", '{"a":1}', '{"log":{"entries":3}}'):
                p.write_text(text)
                with self.assertRaises(h.HarError):
                    h.load_har(p)


class Outputs(unittest.TestCase):
    def setUp(self):
        self.s = h.build(json.loads(SHOP.read_text(encoding="utf-8")))

    def test_plain_english_names(self):
        n = h.plain_name
        self.assertEqual(n("GET", "/api/v1/beans"), "List beans")
        self.assertEqual(n("GET", "/api/v1/beans/{id}"), "Get one bean")
        self.assertEqual(n("GET", "/api/v1/beans/{id}/reviews"), "List reviews of a bean")
        self.assertEqual(n("POST", "/api/v1/cart/items"), "Add an item to the cart")
        self.assertEqual(n("POST", "/api/v1/auth/login"), "Log in")
        self.assertEqual(n("DELETE", "/orders/{id}"), "Delete an order")
        self.assertEqual(n("PATCH", "/categories/{id}"), "Update a category")

    def test_openapi_has_routes_params_schema_and_no_values(self):
        o = h.openapi(self.s)
        self.assertEqual(o["openapi"], "3.0.3")
        get = o["paths"]["/api/v1/beans/{id}"]["get"]
        self.assertEqual(get["parameters"][0]["name"], "id")
        schema = get["responses"]["200"]["content"]["application/json"]["schema"]
        self.assertEqual(schema["properties"]["price"]["type"], "number")
        self.assertIn("name", schema["required"])
        login = o["paths"]["/api/v1/auth/login"]["post"]
        self.assertIn("password", login["requestBody"]["content"]["application/json"]["schema"]["properties"])
        self.assertNotIn("hunter2", json.dumps(o))

    def test_openapi_repeated_ids_get_unique_names(self):
        s = build(ent("GET", "https://a.test/a/1/b/2"))
        self.assertEqual(list(h.openapi(s)["paths"]), ["/a/{id}/b/{id2}"])

    def test_map_is_one_safe_file(self):
        s = build(ent("GET", "https://a.test/x", body='{"a":"</script><img src=x onerror=alert(1)>"}'))
        page = h.map_html(s)
        self.assertNotIn("</script><img", page)
        self.assertNotIn("<script src", page)
        self.assertNotIn("<link", page)

    def test_journey_follows_pages(self):
        j = h.journey(self.s)
        self.assertEqual(j[0]["title"], "Browse the shop")
        self.assertEqual(j[0]["steps"][0], "GET /api/v1/beans")
        self.assertIn("POST /api/v1/auth/login", j[1]["steps"])


class ServerAndCheck(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.s = h.build(json.loads(SHOP.read_text(encoding="utf-8")))
        cls.srv = h.make_server(cls.s, 0)
        cls.url = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def req(self, path, method="GET", headers=None, data=None):
        rq = urllib.request.Request(self.url + path, method=method, headers=headers or {}, data=data)
        try:
            with urllib.request.urlopen(rq, timeout=10) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    def test_real_http_answers(self):
        st, hd, body = self.req("/api/v1/beans/2")
        self.assertEqual(st, 200)
        self.assertEqual(hd["Content-Type"], "application/json")
        self.assertEqual(hd["X-Total-Count"], "3")
        self.assertNotIn("Set-Cookie", hd)
        self.assertEqual(json.loads(body)["id"], 2)

    def test_head_has_no_body(self):
        st, _, body = self.req("/api/v1/beans/2", "HEAD")
        self.assertEqual((st, body), (200, b""))

    def test_miss_is_json_404_and_logged(self):
        st, _, body = self.req("/nope-nothing")
        self.assertEqual(st, 404)
        self.assertEqual(json.loads(body)["har2clone"], "path_not_recorded")
        self.assertIn(b"/nope-nothing", self.req("/__har2clone/misses")[2])

    def test_local_front_end_gets_cors_and_other_sites_do_not(self):
        origin = "http://localhost:5173"
        st, hd, _ = self.req("/api/v1/beans/2", "OPTIONS", {"Origin": origin, "Access-Control-Request-Headers": "x-a"})
        self.assertEqual((st, hd["Access-Control-Allow-Origin"], hd["Access-Control-Allow-Headers"]), (204, origin, "x-a"))
        self.assertEqual(self.req("/api/v1/beans/2", headers={"Origin": "https://evil.example"})[0], 403)

    def test_foreign_host_header_is_refused(self):
        self.assertEqual(self.req("/api/v1/beans/2", headers={"Host": "evil.example"})[0], 403)

    def test_check_passes_against_the_stand_in_and_catches_a_changed_clone(self):
        rows = h.check(self.s, self.url)
        self.assertTrue(rows and all(r["ok"] for r in rows), rows)
        changed = json.loads(json.dumps(self.s))
        for r in changed["routes"]:
            if r["template"] == "/api/v1/beans/{id}":
                for smp in r["samples"]:
                    smp["text"] = '{"id": "one"}'
        srv = h.make_server(changed, 0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            bad = [r for r in h.check(self.s, f"http://127.0.0.1:{srv.server_address[1]}") if not r["ok"]]
        finally:
            srv.shutdown()
            srv.server_close()
        self.assertEqual({r["route"] for r in bad}, {"GET /api/v1/beans/{id}"})
        self.assertTrue(any("name" in p for p in bad[0]["problems"]))
        self.assertTrue(any("id" in p for p in bad[0]["problems"]))

    def test_check_compares_a_recorded_redirect_without_following_it(self):
        s = build(ent("GET", "https://a.test/redir", status=302, body="", headers=[("Location", "/h")]), ent("GET", "https://a.test/h"))
        srv = h.make_server(s, 0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            rows = h.check(s, f"http://127.0.0.1:{srv.server_address[1]}")
        finally:
            srv.shutdown()
            srv.server_close()
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r["ok"] for r in rows), rows)

    def test_check_never_touches_remote_or_sends_writes(self):
        with self.assertRaises(ValueError):
            h.check(self.s, "https://api.brewline.example")
        self.assertFalse([r for r in h.check(self.s, self.url) if r["route"].startswith("POST")])


class Cli(unittest.TestCase):
    def run_cli(self, *args):
        env = {k: v for k, v in os.environ.items() if k not in ("PYTHONUTF8", "PYTHONIOENCODING")}
        return subprocess.run([sys.executable, str(ROOT / "har2clone.py"), *args], capture_output=True, env=env, timeout=120)

    def test_build_writes_all_files_with_utf8_on_any_console(self):
        with tempfile.TemporaryDirectory() as d:
            r = self.run_cli("build", str(SHOP), "-o", d)
            self.assertEqual(r.returncode, 0, r.stderr)
            r.stdout.decode("utf-8")
            for f in ("standin.json", "openapi.json", "map.html", "REBUILD.md"):
                self.assertTrue(pathlib.Path(d, f).stat().st_size > 100, f)
            self.assertIn("très doux ☕", pathlib.Path(d, "standin.json").read_text(encoding="utf-8"))
            self.assertNotIn("hunter2", "".join(p.read_text(encoding="utf-8") for p in pathlib.Path(d).iterdir()))

    def test_demo_exits_zero(self):
        r = self.run_cli("--demo")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(b"check", r.stdout)

    def test_missing_file_is_exit_2_without_traceback(self):
        r = self.run_cli("build", "nope.har", "-o", "x-unused")
        self.assertEqual(r.returncode, 2)
        self.assertNotIn(b"Traceback", r.stderr)


if __name__ == "__main__":
    unittest.main()
