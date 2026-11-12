"""Finto portale Plone per la prova end-to-end. Stato in stato.json, riletto a ogni richiesta."""
import json, sys, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
STATO = Path(sys.argv[1]); BASE = "http://127.0.0.1:8890"
hits = Path(sys.argv[2])  # registro dei download

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, body, ctype="application/json"):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code); self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        st = json.loads(STATO.read_text()); u = urllib.parse.urlparse(self.path)
        if u.path == "/robots.txt": return self._send(200, "User-agent: *\nAllow: /\n", "text/plain")
        if u.path == "/++api++/@search":
            q = urllib.parse.parse_qs(u.query); b = int(q["b_start"][0]); n = int(q["b_size"][0])
            since = q.get("modified.query", [None])[0]
            items = [d for d in st["docs"] if d["listed"] and (not since or d["modified"] >= since)]
            out = [{"@id": f"{BASE}/{d['path']}", "UID": d["uid"], "@type": d["type"], "Title": d["title"],
                    "modified": d["modified"], "getId": d["path"].split("/")[-1], "mime_type": d.get("mime", "text/plain"),
                    "getPath": f"/formazionelavoro/{d['path']}", "review_state": "published"} for d in items[b:b+n]]
            return self._send(200, json.dumps({"items_total": len(items), "items": out}))
        for d in st["docs"]:
            if u.path == f"/++api++/{d['path']}":
                return self._send(200 if d["exists"] else 404, json.dumps({"UID": d["uid"]}))
            if u.path in (f"/{d['path']}/@@download/file", f"/{d['path']}"):
                with hits.open("a") as f: f.write(d["path"] + "\n")
                if not d["exists"] or d.get("download_404"): return self._send(404, "no", "text/plain")
                if d["type"] == "Document":
                    return self._send(200, f"<html><body><main><h1>{d['title']}</h1><p>{d['text']}</p></main></body></html>", "text/html")
                return self._send(200, d["text"], d.get("mime", "text/plain"))
        self._send(404, "no", "text/plain")
ThreadingHTTPServer(("127.0.0.1", 8890), H).serve_forever()
