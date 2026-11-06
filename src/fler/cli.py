"""Riga di comando: ingestione, diagnostica, interrogazione."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

from .catalog import Catalog
from .config import settings


def _log(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)-18s %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def cmd_ingest(a) -> int:
    from .pipeline import Pipeline, load_sources

    srcs = load_sources()
    if a.source:
        srcs = [s for s in srcs if s["id"] in a.source]
        if not srcs:
            print("Nessuna sorgente attiva con quell'id.", file=sys.stderr)
            return 1
    print("Sorgenti: " + ", ".join(s["id"] for s in srcs))
    p = Pipeline()
    stats = asyncio.run(p.run(
        mode="full" if a.full else "incremental",
        sources=srcs, limit=a.limit, skip_embed=a.no_embed,
    ))
    print(json.dumps(stats, indent=2, ensure_ascii=False))
    esito = stats.get("esito")
    if esito == "gia_in_corso":
        print("\nUn'altra ingestione e' in corso: nulla da fare.")
        return 0
    if esito != "ok":
        # codice di uscita diverso da zero: e' cosi' che cron e lo scheduler
        # possono accorgersi che il giro non e' andato a buon fine
        print("\nATTENZIONE: giro incompleto. Il punto di ripartenza non e' stato")
        print("avanzato, quindi il prossimo 'fler ingest' riprendera' da dove serve.")
        return 2
    return 0


def cmd_embed(a) -> int:
    from .pipeline import Pipeline

    p = Pipeline()
    p.embed_pending()
    print(json.dumps(p.stats, indent=2, ensure_ascii=False))
    return 0


def cmd_stato(a) -> int:
    cat = Catalog(settings.catalog_path)
    st = cat.stats()
    try:
        from .store import VectorStore
        st["vettori"] = VectorStore().count()
    except Exception as exc:
        st["vettori"] = f"non disponibile ({exc})"
    print(json.dumps(st, indent=2, ensure_ascii=False))
    rows = cat.conn.execute(
        "SELECT status, COUNT(*) n FROM documents GROUP BY status ORDER BY n DESC"
    ).fetchall()
    if st.get("ultimo_esito") and st["ultimo_esito"] != "ok":
        print("\n!! L'ultimo aggiornamento e' risultato PARZIALE: vedi la tabella runs")
        print("   docker compose run --rm web python -c \"import sqlite3;"
              "print(*[r[0] for r in sqlite3.connect('/data/catalog.sqlite3')"
              ".execute('SELECT stats_json FROM runs ORDER BY run_id DESC LIMIT 3')],sep=chr(10))\"")
    print("\nStato dei documenti:")
    for r in rows:
        print(f"  {r['status']:<12} {r['n']:>7}")
    err = cat.conn.execute(
        "SELECT url, error FROM documents WHERE status IN ('error','empty') "
        "AND error IS NOT NULL ORDER BY last_fetched DESC LIMIT 10"
    ).fetchall()
    if err:
        print("\nUltimi problemi:")
        for r in err:
            print(f"  {r['error'][:90]}\n    {r['url']}")
    return 0


def cmd_cerca(a) -> int:
    """Ricerca senza generazione: il comando da usare per capire se un problema
    sta nel recupero o nella risposta."""
    from .retrieve import Retriever

    r = Retriever()
    res = r.search(a.query, k=a.k)
    if res.refs:
        print("Riferimenti d'atto riconosciuti: " + ", ".join(str(x) for x in res.refs))
    for d in res.degraded:
        print(f"!! {d}")
    ok, motivo = r.is_confident(res)
    print(f"Il sistema {'risponderebbe' if ok else 'SI ASTERREBBE'}: {motivo}\n")

    for h in res.hits:
        d = h.data or {}
        flag = "  [rif. esatto]" if h.exact_ref else ("  [contiguo]" if h.neighbour else "")
        sim = d.get("similarita")
        sim_s = f" sim={sim:.2f}" if isinstance(sim, float) else ""
        stato = []
        if d.get("via_ocr"):
            stato.append("OCR")
        if d.get("expires") and str(d["expires"])[:10] < __import__("datetime").date.today().isoformat():
            stato.append("SCADUTO")
        st = "  [" + ", ".join(stato) + "]" if stato else ""
        print(f"\n{h.score:.4f}{sim_s}{flag}{st}  {d.get('title')}")
        print(f"   {d.get('url')}" + (f"  (pag. {d['page']})" if d.get("page") else ""))
        print("   " + (d.get("text") or "")[:220].replace("\n", " "))
    return 0


def cmd_chiedi(a) -> int:
    from .answer import Assistant

    ans = Assistant().ask(a.query)
    print("\n" + ans.text + "\n")
    print("─" * 70)
    print(f"affidabilita': {ans.confidence}   fonti: {len(ans.sources)}   "
          f"{ans.latency:.1f}s   ({ans.reason})")
    for w in ans.warnings:
        print(f"  ! {w}")
    for s in ans.sources:
        if s.cited:
            pg = f" pag. {s.page}" if s.page else ""
            print(f"  [{s.n}] {s.title}{pg}\n       {s.download_url or s.url}")
    return 0


def cmd_serve(a) -> int:
    import uvicorn

    uvicorn.run("fler.api:app", host=a.host, port=a.port, log_level="info")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="fler",
        description="Assistente documentale sulla formazione e lavoro della Regione Emilia-Romagna",
    )
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("ingest", help="scarica e indicizza la documentazione")
    p.add_argument("--full", action="store_true", help="scansione completa invece che incrementale")
    p.add_argument("--source", action="append", help="limita a una sorgente (ripetibile)")
    p.add_argument("--limit", type=int, help="massimo di documenti da elaborare")
    p.add_argument("--no-embed", action="store_true", help="non calcolare gli embedding")
    p.set_defaults(f=cmd_ingest)

    p = sub.add_parser("embed", help="calcola gli embedding mancanti")
    p.set_defaults(f=cmd_embed)

    p = sub.add_parser("stato", help="stato dell'indice")
    p.set_defaults(f=cmd_stato)

    p = sub.add_parser("cerca", help="ricerca senza generazione")
    p.add_argument("query")
    p.add_argument("-k", type=int, default=10)
    p.set_defaults(f=cmd_cerca)

    p = sub.add_parser("chiedi", help="domanda con risposta citata")
    p.add_argument("query")
    p.set_defaults(f=cmd_chiedi)

    p = sub.add_parser("serve", help="avvia il servizio web")
    p.add_argument("--host", default=settings.host)
    p.add_argument("--port", type=int, default=settings.port)
    p.set_defaults(f=cmd_serve)

    a = ap.parse_args()
    _log(a.verbose)
    settings.ensure_dirs()
    return a.f(a)


if __name__ == "__main__":
    raise SystemExit(main())
