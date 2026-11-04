#!/usr/bin/env python3
"""har2clone: turn a browser recording (HAR) of a web app into an API map, an OpenAPI spec,
a working stand-in backend and a check that proves your rebuild answers the same way.

Python standard library only. Stand-in design ported from Craft Anything (MIT OR Apache-2.0);
plain-English route names and the one-file map idea from Floorplan (MIT).
"""
import argparse, base64, binascii, html, json, pathlib, re, sys, tempfile, threading, urllib.error, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, quote, urlsplit

VERSION = 1
DROPPED = "[dropped]"
ID = "{id}"
MIN_HEX_ID, MIN_TOKEN_ID, MIN_SIBLINGS = 16, 20, 3
MAX_SAMPLES, MAX_BODY, MAX_REQUEST = 16, 4 * 1024 * 1024, 16 * 1024 * 1024

CREDENTIAL_HEADERS = ("cookie", "auth", "token", "secret", "password", "credential", "csrf", "xsrf", "session", "signature", "api-key", "apikey", "-key")
TRANSPORT_HEADERS = {"connection", "keep-alive", "te", "trailer", "transfer-encoding", "upgrade", "content-length", "content-encoding",
                     "content-type", "host", "date", "alt-svc", "strict-transport-security", "content-security-policy",
                     "content-security-policy-report-only", "expect-ct", "nel", "report-to", "reporting-endpoints", "public-key-pins",
                     "server-timing", "via"}
CREDENTIAL_PARAMS = {"token", "access_token", "id_token", "refresh_token", "auth", "authorization", "key", "api_key", "apikey", "secret",
                     "client_secret", "password", "passwd", "pwd", "session", "sessionid", "sid", "sig", "signature", "code",
                     "x-amz-signature", "x-amz-credential", "x-amz-security-token"}
NAMESPACES = {"", "api", "apis", "rest", "rpc", "graphql", "gql", "trpc", "public", "internal", "services", "service", "latest", "stable",
              "beta", "alpha", ID}
STATIC_TYPES = {"document", "script", "stylesheet", "image", "font", "media", "manifest", "texttrack"}
STATIC_MIMES = ("text/html", "text/css", "javascript", "ecmascript", "font/", "image/", "audio/", "video/", "application/wasm")
CODE_MIMES = ("text/html", "text/css", "javascript", "ecmascript")

_B = r"(?<![A-Za-z0-9_\-])"
SECRET_FORMS = re.compile(
    _B + r"(?:eyJ[A-Za-z0-9_\-]{5,}\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+"
    r"|sk-(?=[A-Za-z0-9_\-]*\d)[A-Za-z0-9_\-]{5,}|sk-[A-Za-z0-9_\-]{9,}"
    r"|(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{4,}|whsec_[A-Za-z0-9]{4,}|gh[pousr]_[A-Za-z0-9]{4,}|github_pat_[A-Za-z0-9_]{4,}"
    r"|xox[abps]-[A-Za-z0-9\-]{4,}|glpat-[A-Za-z0-9_\-]{4,}|(?:AKIA|ASIA)[A-Z0-9]{12,}|AIza[A-Za-z0-9_\-]{26,}|npm_[A-Za-z0-9]{16,})")
BEARER = re.compile(r"\bBearer\s+[A-Za-z0-9_\-.~+/=]{8,}")
BASIC = re.compile(r"\bBasic\s+(?=[A-Za-z0-9+/=]*[0-9=+/])[A-Za-z0-9+/=]{8,}")
USER_INFO = re.compile(r"(\b[a-z][a-z0-9+.\-]*://)[^/\s@\"']+@", re.I)
URL_QUERY = re.compile(r"\bhttps?://[^\s\"'<>]+", re.I)
FREE_PARAM = re.compile(r"(?<![A-Za-z0-9_\-.])(access_token|id_token|refresh_token|api_key|apikey|client_secret|password|passwd|pwd|sig|signature|token)=([^&\s\"'<>;]+)", re.I)


class HarError(Exception):
    """The file is not a usable HAR recording."""


def looks_secret(t):
    return bool(SECRET_FORMS.fullmatch(t))


def is_uuid(s):
    return bool(re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", s))


def is_id_segment(s):
    """A path segment that is an id by form alone: a number, UUID, long hex, or a long letter-digit mix."""
    if not s or not re.fullmatch(r"[A-Za-z0-9_\-]+", s):
        return False
    digits, letters = any(c.isdigit() for c in s), any(c.isalpha() for c in s)
    return s.isdigit() or is_uuid(s) or (len(s) >= MIN_HEX_ID and digits and all(c in "0123456789abcdefABCDEF" for c in s)) \
        or (len(s) >= MIN_TOKEN_ID and digits and letters)


def is_namespace(seg):
    l = seg.lower()
    return l in NAMESPACES or bool(re.fullmatch(r"v[0-9.]+", l))


def is_credential_param(name):
    l = name.lower()
    return l in CREDENTIAL_PARAMS or l.endswith("token") or "password" in l or "secret" in l


def is_secret_json_key(k):
    l = k.lower().replace("-", "_")
    return l in ("authorization", "cookie", "set_cookie", "api_key", "apikey", "private_key") or l.endswith("token") \
        or "password" in l or "passwd" in l or "secret" in l or "credential" in l


class Scrub:
    """Takes secrets out of what is kept, and counts what it took. Values are never recorded."""

    def __init__(self, hidden=()):
        spellings = set()
        for v in hidden:
            if v:
                spellings |= {v, quote(v, safe=""), quote(v), base64.b64encode(v.encode()).decode().rstrip("=")}
        # Longest first, so a value inside a longer spelling cannot leave a tail behind.
        self.needles = sorted(spellings, key=len, reverse=True)
        self.dropped = dict(headers=0, query_values=0, json_values=0, text_tokens=0, path_segments=0, hidden_values=0, bodies=0, url_userinfo=0)

    def _hide(self, s):
        for n in self.needles:
            if n in s:
                self.dropped["hidden_values"] += s.count(n)
                s = s.replace(n, DROPPED)
        return s

    def _sub(self, regex, s, repl=DROPPED):
        s, n = regex.subn(repl, s)
        self.dropped["text_tokens"] += n
        return s

    def _url_params(self, m):
        return FREE_PARAM.sub(lambda p: self._count_param(p), m.group(0))

    def _count_param(self, p):
        self.dropped["text_tokens"] += 1
        return f"{p.group(1)}={DROPPED}"

    def text(self, s):
        """Free text in an API answer: every form of credential goes, ordinary words stay."""
        s = self.code(s)
        s = self._sub(BEARER, s, "Bearer " + DROPPED)
        s = self._sub(BASIC, s, "Basic " + DROPPED)
        return FREE_PARAM.sub(self._count_param, s)

    def code(self, s):
        """A page, script or style sheet: `x.token=y` is code there, so only forms that are credentials anywhere go."""
        s = self._hide(s)
        s = self._sub(USER_INFO, s, r"\1")
        s = URL_QUERY.sub(self._url_params, s)
        return self._sub(SECRET_FORMS, s)

    def json(self, v):
        if isinstance(v, dict):
            out = {}
            for k, x in v.items():
                if is_secret_json_key(k) and isinstance(x, str) and x:
                    self.dropped["json_values"] += 1
                    out[k] = DROPPED
                else:
                    out[k] = self.json(x)
            return out
        if isinstance(v, list):
            return [self.json(x) for x in v]
        return self.text(v) if isinstance(v, str) else v

    def body(self, text, mime):
        m = (mime or "").lower()
        if any(c in m for c in CODE_MIMES):
            return self.code(text)
        try:
            v = json.loads(text)
        except ValueError:
            return self.text(text)
        clean = self.json(v)
        # Untouched bodies stay byte for byte: re-encoding would change spacing and escapes.
        return text if clean == v else json.dumps(clean, ensure_ascii=False)

    def path(self, path, count=True):
        out = []
        for seg in path.split("/"):
            if looks_secret(seg) or self._hide(seg) != seg:
                self.dropped["path_segments"] += count
                out.append(ID)
            else:
                out.append(seg)
        return "/".join(out)

    def query(self, pairs, count=True):
        names, kept = [], []
        for k, v in pairs:
            if k not in names:
                names.append(k)
            if is_credential_param(k) or looks_secret(v) or self._hide(v) != v:
                self.dropped["query_values"] += count
                v = DROPPED
            kept.append((k, v))
        kept.sort()
        return sorted(names), "&".join(f"{quote(k, safe='')}={quote(v, safe='')}" for k, v in kept)

    def headers(self, headers):
        out = []
        for h in headers or []:
            name, value = str(h.get("name", "")), str(h.get("value", ""))
            l = name.lower()
            if l in TRANSPORT_HEADERS or l.startswith(("proxy-", "access-control-", ":")):
                continue
            if any(w in l for w in CREDENTIAL_HEADERS):
                self.dropped["headers"] += 1
                continue
            out.append((name, self.text(value)))
        return out


def request_key(body):
    """What tells requests to one URL apart, without any of their values: GraphQL operations or JSON key names."""
    if not body or len(body) > 1024 * 1024:
        return None
    try:
        v = json.loads(body)
    except ValueError:
        return None

    def op(o):
        if o.get("operationName"):
            return str(o["operationName"])
        m = re.match(r"\s*(?:query|mutation|subscription)\s+([A-Za-z_]\w*)", str(o.get("query", "")))
        return m.group(1) if m else None

    if isinstance(v, dict) and v:
        name = op(v)
        return f"op:{name}" if name else "keys:" + ",".join(sorted(k for k in v if not looks_secret(k))[:16])
    if isinstance(v, list):
        names = [n for n in (op(x) for x in v[:16] if isinstance(x, dict)) if n]
        return "op:" + ",".join(names) if names else None
    return None


def schema_of(v, depth=0):
    """The type of a JSON value. Holds names and types only, never the values."""
    if depth > 12 or v is None:
        return {"nullable": True}
    if isinstance(v, bool):
        return {"type": "boolean"}
    if isinstance(v, int):
        return {"type": "integer"}
    if isinstance(v, float):
        return {"type": "number"}
    if isinstance(v, str):
        return {"type": "string"}
    if isinstance(v, list):
        items = {}
        for x in v[:20]:
            items = merge_schema(items, schema_of(x, depth + 1)) if items else schema_of(x, depth + 1)
        return {"type": "array", "items": items}
    props = {k: schema_of(x, depth + 1) for k, x in v.items() if not looks_secret(k)}
    return {"type": "object", "properties": props, "required": sorted(props)}


def merge_schema(a, b):
    if not a or not b:
        return a or b
    out = dict(a)
    if b.get("nullable"):
        out["nullable"] = True
    ta, tb = a.get("type"), b.get("type")
    if ta is None:
        out = dict(b, **({"nullable": True} if a.get("nullable") else {}))
    elif tb is not None and ta != tb:
        if {ta, tb} == {"integer", "number"}:
            out["type"] = "number"
        else:
            return {"nullable": True} if out.get("nullable") else {}
    elif ta == "object" and tb == "object":
        pa, pb = a["properties"], b["properties"]
        out["properties"] = {k: merge_schema(pa[k], pb[k]) if k in pa and k in pb else pa.get(k, pb.get(k)) for k in {**pa, **pb}}
        out["required"] = sorted(set(a["required"]) & set(b["required"]))
    elif ta == "array" and tb == "array":
        out["items"] = merge_schema(a["items"], b["items"])
    return out


def json_or_none(text):
    try:
        return json.loads(text) if text else None
    except ValueError:
        return None


def body_shape(text):
    v = json_or_none(text)
    if isinstance(v, dict) and v:
        return "o:" + ",".join(v)
    if isinstance(v, list) and v and isinstance(v[0], dict) and v[0]:
        return "a:" + ",".join(v[0])
    return None


def merge_siblings(seen):
    """Word-like ids need evidence: two values with the same text after them, or as the last segment
    three values that all answer in the same shape. Names under a namespace and dotted names stay apart."""
    for pos in range(max((len(s["segs"]) for s in seen), default=0) - 1, 0, -1):
        groups = {}
        for s in seen:
            if len(s["segs"]) > pos:
                key = (s["method"], len(s["segs"]), "/".join(x for j, x in enumerate(s["segs"]) if j != pos))
                groups.setdefault(key, []).append(s)
        for (_, length, _), members in groups.items():
            if is_namespace(members[0]["segs"][pos - 1]):
                continue
            values = {m["segs"][pos] for m in members}
            if any(not v or "." in v for v in values):
                continue
            shapes = {m["shape"] for m in members}
            if (pos + 1 < length and len(values) >= 2) or (len(values) >= MIN_SIBLINGS and len(shapes) == 1 and None not in shapes):
                for m in members:
                    m["segs"][pos] = ID


def load_har(path):
    try:
        data = json.loads(pathlib.Path(path).read_text(encoding="utf-8-sig"))
    except OSError as e:
        raise HarError(f"cannot read {path}: {e.strerror}")
    except ValueError as e:
        raise HarError(f"{path} is not JSON: {e}")
    entries = data.get("log", {}).get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        raise HarError(f"{path} has no log.entries list, so it is not a HAR file")
    return data


def is_static(e, mime):
    kind = str(e.get("_resourceType") or "").lower()
    return kind in STATIC_TYPES if kind else any(m in mime for m in STATIC_MIMES)


def make_sample(s, scrub):
    """One recorded answer. A 304 stands for the body the browser had cached; without one it cannot be replayed."""
    content = s["entry"]["response"].get("content") or {}
    mime = str(content.get("mimeType") or "").split(";")[0].strip()
    sample = dict(path=s["path"], query=s["query"], status=s["status"], content_type=mime or None,
                  headers=scrub.headers(s["entry"]["response"].get("headers")))
    if s["key"]:
        sample["request_key"] = s["key"]
    raw = content.get("text")
    if not raw:
        return sample
    if str(content.get("encoding") or "").lower() == "base64":
        try:
            data = base64.b64decode(raw, validate=False)
        except (binascii.Error, ValueError):
            sample["body_left_out"] = "undecodable"
            return sample
        text = None
        if any(m in mime for m in ("json", "text", "xml", "javascript")):
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                pass
        if text is None:
            if len(data) > MAX_BODY:
                sample["body_left_out"] = "too_large"
                return sample
            latin = data.decode("latin-1")
            if scrub.code(latin) != latin:
                scrub.dropped["bodies"] += 1
                sample["body_left_out"] = "held_secret"
            else:
                sample["base64"] = raw
            return sample
        raw = text
    if len(raw) > MAX_BODY:
        sample["body_left_out"] = "too_large"
        return sample
    sample["text"] = scrub.body(raw, mime)
    return sample


def build(har, hidden=(), only_hosts=(), include_static=False, source=None):
    scrub = Scrub(hidden)
    skipped = dict(static=0, not_http=0, no_response=0, uncached_304=0, other_host=0)
    seen, hosts = [], set()
    pages = {p.get("id"): p.get("title") or p.get("id") for p in har["log"].get("pages") or []}
    for e in har["log"]["entries"]:
        if not isinstance(e, dict):
            skipped["not_http"] += 1
            continue
        req, resp = e.get("request") or {}, e.get("response") or {}
        u = urlsplit(str(req.get("url") or ""))
        if u.scheme not in ("http", "https"):
            skipped["not_http"] += 1
            continue
        status = int(resp.get("status") or 0)
        if status <= 0:
            skipped["no_response"] += 1
            continue
        mime = str((resp.get("content") or {}).get("mimeType") or "").lower()
        if not include_static and is_static(e, mime):
            skipped["static"] += 1
            continue
        host = u.netloc.rpartition("@")[2]  # user:pass@ is a credential, never a host name
        if only_hosts and host not in only_hosts and u.hostname not in only_hosts:
            skipped["other_host"] += 1
            continue
        path = scrub.path(u.path or "/")
        names, query = scrub.query(parse_qsl(u.query, keep_blank_values=True))
        post = str((req.get("postData") or {}).get("text") or "")
        text = (resp.get("content") or {}).get("text")
        # Evidence for cached answers: the body the browser reused turns a 304 into a real 200.
        if status == 304 and text:
            status = 200
        elif status == 304:
            skipped["uncached_304"] += 1
        if "@" in u.netloc:
            scrub.dropped["url_userinfo"] += 1
        hosts.add(host)
        seen.append(dict(entry=e, method=str(req.get("method") or "GET").upper(), host=host, path=path, query=query, names=names,
                         segs=[ID if is_id_segment(x) else x for x in path.split("/")], shape=body_shape(text), post=post,
                         key=request_key(post.encode()), status=status, page=pages.get(e.get("pageref"), e.get("pageref"))))
    merge_siblings(seen)
    routes, order = {}, []
    for s in seen:
        template = "/".join(s["segs"]) or "/"
        r = routes.get((s["method"], template))
        if r is None:
            r = routes[(s["method"], template)] = dict(
                method=s["method"], template=template, name=plain_name(s["method"], template), hosts=[], statuses=[], query_names=[],
                requests=0, samples=[], samples_left_out=0)
        r["requests"] += 1
        for field, values in (("hosts", [s["host"]]), ("statuses", [s["status"]]), ("query_names", s["names"])):
            r[field] = sorted(set(r[field]) | set(values))
        v = json_or_none(s["post"])
        if isinstance(v, (dict, list)):
            r["request_schema"] = merge_schema(r.get("request_schema"), schema_of(v))
        if len(r["samples"]) < MAX_SAMPLES:
            r["samples"].append(make_sample(s, scrub))
        else:
            r["samples_left_out"] += 1
        order.append((s["page"], f"{s['method']} {template}"))
    return dict(version=VERSION, source=source, hosts=sorted(hosts), order=order, dropped=scrub.dropped, skipped=skipped,
                routes=sorted(routes.values(), key=lambda r: (r["template"], r["method"])))


def template_fits(template, path):
    t, p = template.split("/"), path.split("/")
    return len(t) == len(p) and all((a == ID and b) or a == b for a, b in zip(t, p))


class Replayer:
    """Picks the recorded answer for a request. Replay is read-only: a write answers as recorded and changes nothing."""

    def __init__(self, standin):
        self.standin, self.turns, self.misses, self.lock = standin, {}, [], threading.Lock()

    def answer(self, method, path, query, body):
        method = "GET" if method.upper() == "HEAD" else method.upper()
        scrub = Scrub()
        path = scrub.path(path, count=False)
        query = scrub.query(parse_qsl(query, keep_blank_values=True), count=False)[1]
        key = request_key(body)
        same = [r for r in self.standin["routes"] if r["method"] == method]
        wrong_op, picked = False, None
        passes = (("query", lambda r, s: s["path"] == path and s["query"] == query, same),
                  ("path", lambda r, s: s["path"] == path, same),
                  ("template", lambda r, s: True, sorted((r for r in same if template_fits(r["template"], path)),
                                                         key=lambda r: -sum(x != ID for x in r["template"].split("/")))))
        for how, test, routes in passes:
            for r in routes:
                found = [i for i, s in enumerate(r["samples"]) if test(r, s)]
                if not found:
                    continue
                fit = self._fitting(found, r, key)
                if fit:
                    picked = (r, how, fit)
                    break
                wrong_op = True
            if picked:
                break
        if not picked:
            known = any(template_fits(r["template"], path) or any(s["path"] == path for s in r["samples"]) for r in self.standin["routes"])
            why = "operation_not_recorded" if wrong_op else "method_not_recorded" if known else "path_not_recorded"
            return self._miss(why, method, path)
        r, how, fit = picked
        fit = [i for i in fit if r["samples"][i]["status"] != 304]
        if not fit:
            return self._miss("cached_body_unavailable", method, path)
        with self.lock:
            k = (r["method"], r["template"], how, fit[0])
            turn = self.turns.get(k, 0)
            self.turns[k] = turn + 1
        s = r["samples"][fit[turn % len(fit)]]
        data = base64.b64decode(s["base64"]) if s.get("base64") else (s.get("text") or "").encode("utf-8")
        return dict(route=f"{r['method']} {r['template']}", how=how, status=s["status"], content_type=s.get("content_type"),
                    headers=s.get("headers") or [], body=data)

    @staticmethod
    def _fitting(found, route, key):
        """A named GraphQL operation must be one that was recorded, since another's answer is the wrong data."""
        key_of = lambda i: route["samples"][i].get("request_key")
        same = [i for i in found if key and key_of(i) == key]
        if same:
            return same
        if key and key.startswith("op:") and any((key_of(i) or "").startswith("op:") for i in found):
            return []
        return found

    def _miss(self, why, method, path):
        near = self.nearest(method, path)
        with self.lock:
            self.misses.append(dict(method=method, path=path, reason=why))
        return dict(miss=why, nearest=near)

    def nearest(self, method, path):
        segs = path.split("/")

        def score(r):
            t = r["template"].split("/")
            return (sum(a == b for a, b in zip(t, segs)) + 2 * (r["method"] == method) - abs(len(t) - len(segs)))

        ranked = sorted(self.standin["routes"], key=score, reverse=True)[:5]
        return [f"{r['method']} {r['template']}" for r in ranked]


LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def is_local_origin(origin):
    return urlsplit(origin).hostname in LOCAL_HOSTS or (urlsplit(origin).hostname or "").endswith(".localhost")


def make_server(standin, port):
    """Loopback only. Recorded answers may hold personal data, so other sites and rebound hostnames are refused."""
    replayer = Replayer(standin)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def send(self, status, data=b"", ctype="application/json", extra=()):
            self.send_response(status)
            if ctype:
                self.send_header("Content-Type", ctype)
            for k, v in extra:
                self.send_header(k, v)
            origin = self.headers.get("Origin")
            if origin and is_local_origin(origin):
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Access-Control-Allow-Credentials", "true")
                self.send_header("Vary", "Origin")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def reply_json(self, status, value):
            self.send(status, json.dumps(value, ensure_ascii=False, indent=1).encode("utf-8"))

        def handle_any(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_REQUEST:
                self.close_connection = True
                return self.reply_json(413, {"har2clone": "request_too_large"})
            body = self.rfile.read(length) if length else b""
            host = urlsplit("//" + (self.headers.get("Host") or "")).hostname
            origin = self.headers.get("Origin")
            if host not in LOCAL_HOSTS or (origin and not is_local_origin(origin)):
                return self.reply_json(403, {"har2clone": "only_this_computer", "detail": "the stand-in answers local pages only"})
            u = urlsplit(self.path)
            if u.path == "/__har2clone/misses":
                return self.reply_json(200, replayer.misses)
            if u.path == "/__har2clone/routes":
                return self.reply_json(200, [{"method": r["method"], "template": r["template"], "name": r["name"]} for r in standin["routes"]])
            found = replayer.answer(self.command, u.path, u.query, body)
            if self.command == "OPTIONS" and "miss" in found:
                replayer.misses.pop()
                return self.send(204, ctype=None, extra=[
                    ("Access-Control-Allow-Methods", "GET, HEAD, POST, PUT, PATCH, DELETE, OPTIONS"),
                    ("Access-Control-Allow-Headers", self.headers.get("Access-Control-Request-Headers") or "*")])
            if "miss" in found:
                return self.reply_json(404, {"har2clone": found["miss"], "path": u.path, "nearest": found["nearest"]})
            self.send(found["status"], found["body"], found["content_type"], found["headers"] + [("X-Har2clone-Route", found["route"])])

        do_GET = do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = handle_any

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    return server


def shape_problems(want, got, where="$"):
    """How an answer differs from a recorded shape. Extra fields are fine; missing or retyped ones are not."""
    if not want or not want.get("type"):
        return []
    if not got or not got.get("type"):
        return [] if got and got.get("nullable") and want.get("nullable") else [f"{where}: expected {want['type']}, got null"]
    if want["type"] != got["type"] and {want["type"], got["type"]} != {"integer", "number"}:
        return [f"{where}: expected {want['type']}, got {got['type']}"]
    out = []
    if want["type"] == "object":
        for k in want["required"]:
            if k not in got["properties"]:
                out.append(f"{where}.{k}: missing")
        for k in want["properties"]:
            if k in got["properties"]:
                out += shape_problems(want["properties"][k], got["properties"][k], f"{where}.{k}")
    elif want["type"] == "array" and got.get("items"):
        out += shape_problems(want["items"], got["items"], f"{where}[]")
    return out


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):  # a recorded 302 must be compared as a 302, so it surfaces as an HTTPError
        return None


def check(standin, base):
    """Ask a rebuild every recorded read and compare status and JSON shape. Writes are never sent and only local targets are allowed."""
    if urlsplit(base).hostname not in LOCAL_HOSTS:
        raise ValueError(f"check only talks to this computer, not {base}: it replays recorded requests")
    rows, opener = [], urllib.request.build_opener(NoRedirect)
    for r in standin["routes"]:
        if r["method"] not in ("GET", "HEAD"):
            continue
        for s in r["samples"]:
            if s["status"] == 304:
                continue
            url = base.rstrip("/") + quote(s["path"].replace(ID, "1"), safe="/%") + ("?" + s["query"] if s["query"] else "")
            try:
                with opener.open(urllib.request.Request(url, method=r["method"]), timeout=15) as resp:
                    status, data = resp.status, resp.read()
            except urllib.error.HTTPError as e:
                status, data = e.code, e.read()
            except OSError as e:
                rows.append(dict(route=f"{r['method']} {r['template']}", ok=False, problems=[f"no answer: {e}"]))
                break
            problems = [] if status == s["status"] else [f"status: expected {s['status']}, got {status}"]
            want, got = json_or_none(s.get("text")), json_or_none(data.decode("utf-8", "replace"))
            if want is not None:
                problems += shape_problems(schema_of(want), schema_of(got) if got is not None else {})
            rows.append(dict(route=f"{r['method']} {r['template']}", ok=not problems, problems=problems))
            break  # shortcut: one sample per route, enough for shape; loop samples if per-id answers ever differ in shape
    return rows


IRREGULAR = {"people": "person", "children": "child", "statuses": "status", "addresses": "address", "series": "series", "news": "news", "analysis": "analysis"}
ACTIONS = {"login": "Log in", "signin": "Log in", "sign-in": "Log in", "logout": "Log out", "signout": "Log out", "sign-out": "Log out",
           "signup": "Sign up", "sign-up": "Sign up", "register": "Sign up", "refresh": "Refresh the session", "search": "Search",
           "checkout": "Check out", "pay": "Pay", "upload": "Upload a file", "verify": "Verify", "subscribe": "Subscribe",
           "unsubscribe": "Unsubscribe", "graphql": "Run a GraphQL query", "gql": "Run a GraphQL query", "me": "Get the signed-in user"}


def singular(w):
    l = w.lower()
    if l in IRREGULAR:
        return IRREGULAR[l]
    if l.endswith("ies") and len(l) > 4:
        return l[:-3] + "y"
    if l.endswith(("ches", "shes", "xes", "sses")):
        return l[:-2]
    return l[:-1] if l.endswith("s") and not l.endswith("ss") else l


def a(w):
    return ("an " if w[:1] in "aeiou" else "a ") + w


def words(seg):
    return re.sub(r"[-_]+", " ", re.sub(r"(?<=[a-z])(?=[A-Z])", " ", seg)).lower()


def plain_name(method, template):
    """Say what a route does in plain English, from its method and path alone."""
    segs = [s for s in template.split("/") if s == ID or s in ("graphql", "gql") or (s and not is_namespace(s))]
    if not segs:
        return {"GET": "Open the root", "POST": "Send to the root"}.get(method, f"{method.title()} the root")
    last, ends_id = segs[-1], segs[-1] == ID
    names = [s for s in segs if s != ID]
    if not names:
        return f"{method.title()} an item"
    res = names[-1]
    if res.lower() in ACTIONS and not ends_id:
        return ACTIONS[res.lower()]
    res_w, parent = words(res), names[-2] if len(names) > 1 else None
    one = singular(res_w)
    if method == "GET":
        if ends_id:
            return f"Get one {one}"
        if parent and ID in segs[:-1]:
            return f"List {res_w} of {a(singular(words(parent)))}"
        return f"List {res_w}" if one != res_w else f"Get the {res_w}"
    if method == "POST":
        if parent and not ends_id:
            prep = a(singular(words(parent))) if ID in segs[:-1] else "the " + words(parent)
            return f"Add {a(one)} to {prep}"
        return f"Create {a(one)}" if one != res_w else f"Send to {res_w}"
    verb = {"PUT": "Update", "PATCH": "Update", "DELETE": "Delete"}.get(method, method.title())
    return f"{verb} {a(one)}"


def journey(standin):
    """The recording in order, by page: each route once, where it first appears."""
    out, by_page, seen = [], {}, set()
    for page, step in standin["order"]:
        if (page, step) in seen:
            continue
        seen.add((page, step))
        if page not in by_page:
            by_page[page] = dict(title=page or "Recording", steps=[])
            out.append(by_page[page])
        by_page[page]["steps"].append(step)
    return out


def openapi(standin):
    paths = {}
    for r in standin["routes"]:
        n = 0

        def name_id(_):
            nonlocal n
            n += 1
            return "{id}" if n == 1 else "{id%d}" % n

        path = re.sub(r"\{id\}", name_id, r["template"])
        op = dict(summary=r["name"], parameters=[dict(name="id" if i == 0 else f"id{i + 1}", required=True, schema={"type": "string"}, **{"in": "path"}) for i in range(n)]
                  + [dict(name=q, required=False, schema={"type": "string"}, **{"in": "query"}) for q in r["query_names"]], responses={})
        if r.get("request_schema"):
            op["requestBody"] = {"content": {"application/json": {"schema": r["request_schema"]}}}
        for s in r["samples"]:
            resp = op["responses"].setdefault(str(s["status"]), {"description": "Recorded answer"})
            v = json_or_none(s.get("text"))
            if v is not None:
                media = resp.setdefault("content", {}).setdefault("application/json", {"schema": {}})
                media["schema"] = merge_schema(media["schema"], schema_of(v))
            elif s.get("content_type"):
                resp.setdefault("content", {}).setdefault(s["content_type"], {})
        paths.setdefault(path, {})[r["method"].lower()] = op
    return {"openapi": "3.0.3", "info": {"title": f"API recorded from {', '.join(standin['hosts']) or 'a HAR file'}", "version": "recorded",
                                         "description": "Generated by har2clone from one recording: only what the recording exercised is here."},
            "servers": [{"url": f"https://{h}"} for h in standin["hosts"]], "paths": paths}


MAP_PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>API map</title><style>
:root{--bg:#f7f6f2;--card:#fff;--ink:#1d1c1a;--dim:#6d6a62;--line:#e2dfd6;--get:#0b7a4b;--post:#9a5b00;--put:#2456b3;--patch:#2456b3;--delete:#b3261e}
@media (prefers-color-scheme:dark){:root{--bg:#161513;--card:#1f1e1b;--ink:#efece4;--dim:#a29e93;--line:#34322c;--get:#4cc38a;--post:#e0a243;--put:#7aa5f0;--patch:#7aa5f0;--delete:#f0776e}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}
header{padding:24px 24px 12px}h1{margin:0;font-size:24px}.stats{color:var(--dim);margin-top:4px}
#q{width:100%;max-width:420px;margin-top:12px;padding:8px 12px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--ink);font:inherit}
main{display:grid;grid-template-columns:minmax(0,5fr) minmax(0,6fr);gap:16px;padding:12px 24px 32px}@media (max-width:820px){main{grid-template-columns:1fr}}
h2{font-size:13px;letter-spacing:.06em;text-transform:uppercase;color:var(--dim);margin:18px 0 6px}
button.r{display:block;width:100%;text-align:left;background:var(--card);border:1px solid var(--line);border-radius:8px;padding:8px 10px;margin:0 0 6px;color:inherit;font:inherit;cursor:pointer}
button.r:hover,button.r.on{border-color:var(--ink)}.n{font-weight:600}.p{font:12px/1.4 ui-monospace,Consolas,monospace;color:var(--dim);overflow-wrap:anywhere}
.m{font:700 11px ui-monospace,Consolas,monospace;margin-right:6px}aside{position:sticky;top:12px;align-self:start;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;min-width:0}
pre{overflow:auto;max-height:340px;background:var(--bg);border-radius:8px;padding:10px;font:12px/1.45 ui-monospace,Consolas,monospace;margin:6px 0 0}
.j{margin:0;padding-left:20px}.j li{margin:2px 0}.note{color:var(--dim);font-size:13px}
</style></head><body><header><h1 id="t"></h1><div class="stats" id="s"></div><input id="q" type="search" placeholder="Filter routes" aria-label="Filter routes"></header>
<main><section id="l"></section><aside aria-live="polite"><div id="d"></div><h2>What the recording did</h2><div id="j"></div></aside></main>
<script id="data" type="application/json">@@DATA@@</script><script>
const D=JSON.parse(document.getElementById('data').textContent),$=i=>document.getElementById(i);
const el=(t,c,x)=>{const e=document.createElement(t);if(c)e.className=c;if(x!=null)e.textContent=x;return e};
$('t').textContent='API map of '+(D.hosts.join(', ')||'a recording');
$('s').textContent=D.routes.length+' routes, '+D.requests+' requests. Dropped from the file: '+D.dropped+' secrets.';
function pretty(t){try{return JSON.stringify(JSON.parse(t),null,1)}catch(e){return t||''}}
function show(r,b){document.querySelectorAll('button.r').forEach(x=>x.classList.remove('on'));b.classList.add('on');const d=$('d');d.textContent='';
d.append(el('div','n',r.name),el('div','p',r.method+' '+r.template),el('div','note','Status '+r.statuses.join(', ')+' · '+r.requests+' recorded'+(r.query_names.length?' · query: '+r.query_names.join(', '):'')));
if(r.request_schema){d.append(el('h2','','Request fields'),el('pre','',Object.keys(r.request_schema.properties||{}).join(', ')))}
const s=r.samples[0];if(s){d.append(el('h2','','Recorded answer'),el('pre','',s.text!=null?pretty(s.text):(s.body_left_out?'Body left out: '+s.body_left_out:'Binary body ('+(s.content_type||'unknown type')+')')))}}
function draw(){const q=$('q').value.toLowerCase(),l=$('l');l.textContent='';const g={};
for(const r of D.routes){if(q&&!(r.name+' '+r.method+' '+r.template).toLowerCase().includes(q))continue;(g[r.group]=g[r.group]||[]).push(r)}
for(const k of Object.keys(g).sort()){l.append(el('h2','',k));for(const r of g[k]){const b=el('button','r');const m=el('span','m',r.method);m.style.color='var(--'+r.method.toLowerCase()+',var(--dim))';
b.append(m,el('span','n',r.name),el('div','p',r.template));b.onclick=()=>show(r,b);l.append(b)}}
if(!l.children.length)l.append(el('p','note','No route matches.'))}
for(const p of D.journey){const b=$('j');b.append(el('div','n',p.title));const o=el('ol','j');for(const s of p.steps)o.append(el('li','p',s));b.append(o)}
$('q').oninput=draw;draw();const f=document.querySelector('button.r');if(f)f.click();
</script></body></html>"""


def route_group(template):
    names = [s for s in template.split("/") if s and s != ID and not is_namespace(s)]
    return words(names[0]).capitalize() if names else "Root"


def map_html(standin):
    routes = [dict(r, group=route_group(r["template"]), samples=r["samples"][:1]) for r in standin["routes"]]
    data = dict(hosts=standin["hosts"], routes=routes, requests=len(standin["order"]), journey=journey(standin), dropped=sum(standin["dropped"].values()))
    # "<" is escaped so a recorded body can never close this script block.
    return MAP_PAGE.replace("@@DATA@@", json.dumps(data, ensure_ascii=False).replace("<", "\\u003c"))


def rebuild_brief(standin):
    lines = [f"# Rebuild brief: {', '.join(standin['hosts']) or 'recorded app'}", "",
             "Generated by har2clone from one recording. It covers only what the recording exercised.", "",
             "## How to work", "",
             "1. Run the stand-in so your front end has a backend: `python har2clone.py serve . --port 8787`.",
             "2. Point the front end at `http://localhost:8787`. Replay is read-only: a write answers as recorded and later reads do not change.",
             "3. See what the recording does not cover: `http://localhost:8787/__har2clone/misses`. Record that flow and rebuild, do not guess it.",
             "4. Prove your backend answers the same way: `python har2clone.py check . --against http://localhost:3000`.", "",
             "Build your own code and design. Use this only for software you own or are allowed to study.", "",
             "## Routes", "", "| What it does | Route | Status | Query |", "|---|---|---|---|"]
    for r in standin["routes"]:
        q = ", ".join(r["query_names"]) or "-"
        lines.append(f"| {r['name']} | `{r['method']} {r['template']}` | {', '.join(map(str, r['statuses']))} | {q} |")
    lines += ["", "## What the recording did, in order", ""]
    for p in journey(standin):
        lines += [f"### {p['title']}", ""] + [f"{i}. `{s}`" for i, s in enumerate(p["steps"], 1)] + [""]
    lines += ["## Data shapes", "", "See `openapi.json` for inferred request and response schemas, and `standin.json` for recorded answers."]
    return "\n".join(lines) + "\n"


def write_outputs(standin, out):
    out = pathlib.Path(out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "standin.json").write_text(json.dumps(standin, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "openapi.json").write_text(json.dumps(openapi(standin), ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "map.html").write_text(map_html(standin), encoding="utf-8")
    (out / "REBUILD.md").write_text(rebuild_brief(standin), encoding="utf-8")


def summary(standin, out):
    d, s = standin["dropped"], standin["skipped"]
    print(f"{len(standin['routes'])} routes from {len(standin['order'])} requests on {', '.join(standin['hosts']) or 'no host'}")
    print(f"secrets dropped: {sum(d.values())} ({', '.join(f'{v} {k}' for k, v in d.items() if v) or 'none found'})")
    print(f"skipped: {', '.join(f'{v} {k}' for k, v in s.items() if v) or 'nothing'}")
    print(f"wrote {out}/standin.json, openapi.json, map.html, REBUILD.md")
    print("note: answers stay as recorded, so personal data in them is kept. Use --hide VALUE and read standin.json before you share it.")


def print_check(rows):
    good = sum(r["ok"] for r in rows)
    for r in rows:
        print(("ok    " if r["ok"] else "FAIL  ") + r["route"])
        for p in r["problems"]:
            print("      " + p)
    print(f"check: {good}/{len(rows)} recorded reads answer the same way (writes are not sent)")
    return 0 if good == len(rows) and rows else 1


def load_standin(folder):
    path = pathlib.Path(folder) / "standin.json"
    try:
        s = json.loads(path.read_text(encoding="utf-8"))
    except OSError as e:
        raise HarError(f"cannot read {path}: {e.strerror}. Run `har2clone build` first")
    except ValueError as e:
        raise HarError(f"{path} is not JSON: {e}")
    if s.get("version") != VERSION:
        raise HarError(f"{path} has version {s.get('version')}, this tool reads version {VERSION}")
    return s


def demo():
    """Run the whole loop on the bundled recording: build, serve, replay, check."""
    shop = pathlib.Path(__file__).resolve().parent / "examples" / "shop.har"
    with tempfile.TemporaryDirectory() as d:
        s = build(load_har(shop), source=shop.name)
        write_outputs(s, d)
        summary(s, "<temp>")
        print("\nmap names:")
        for r in s["routes"][:8]:
            print(f"  {r['method']:<6} {r['template']:<34} {r['name']}")
        srv = make_server(s, 0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        with urllib.request.urlopen(base + "/api/v1/beans/2", timeout=10) as r:
            print(f"\nstand-in GET /api/v1/beans/2 -> {r.status} {r.read().decode('utf-8')}")
        code = print_check(check(s, base))
        srv.shutdown()
        srv.server_close()
    return code


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        # Route names and answers are UTF-8 whatever the console code page says.
        stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(prog="har2clone", description=__doc__.split("\n")[0])
    ap.add_argument("--demo", action="store_true", help="run the whole loop on the bundled recording")
    sub = ap.add_subparsers(dest="cmd")
    b = sub.add_parser("build", help="turn a HAR file into standin.json, openapi.json, map.html and REBUILD.md")
    b.add_argument("har")
    b.add_argument("-o", "--out", default="clone")
    b.add_argument("--hide", action="append", default=[], help="a value to remove everywhere, in every spelling (repeatable)")
    b.add_argument("--only-host", action="append", default=[], help="keep only this host (repeatable)")
    b.add_argument("--include-static", action="store_true", help="also keep pages, scripts, styles, images and fonts")
    v = sub.add_parser("serve", help="answer recorded requests on this computer")
    v.add_argument("folder", nargs="?", default="clone")
    v.add_argument("--port", type=int, default=8787)
    c = sub.add_parser("check", help="compare a local rebuild with every recorded read")
    c.add_argument("folder", nargs="?", default="clone")
    c.add_argument("--against", required=True, help="your rebuild, for example http://localhost:3000")
    args = ap.parse_args(argv)
    if args.demo:
        return demo()
    try:
        if args.cmd == "build":
            s = build(load_har(args.har), args.hide, args.only_host, args.include_static, pathlib.Path(args.har).name)
            write_outputs(s, args.out)
            summary(s, args.out)
            return 0
        if args.cmd == "serve":
            srv = make_server(load_standin(args.folder), args.port)
            print(f"stand-in on http://127.0.0.1:{srv.server_address[1]} (misses at /__har2clone/misses). Ctrl+C stops it.", flush=True)
            try:
                srv.serve_forever()
            except KeyboardInterrupt:
                pass
            return 0
        if args.cmd == "check":
            return print_check(check(load_standin(args.folder), args.against))
    except (HarError, ValueError) as e:
        print(f"har2clone: {e}", file=sys.stderr)
        return 2
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
