import asyncio, json, logging, sys, types
sys.path.insert(0, sys.argv[1])
try:
    import selectolax.parser  # noqa
except ImportError:
    sys.modules["selectolax.parser"] = types.SimpleNamespace(HTMLParser=object)
logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
from fler.fetcher import Fetcher
from fler.lotto import Lotto
from fler.remoto import CatalogoRemoto
modo = sys.argv[2]; limite = int(sys.argv[3]) if len(sys.argv) > 3 else None
SRC = [{"id": "formazionelavoro", "kind": "plone", "base_url": "http://127.0.0.1:8890", "enabled": True}]
async def main():
    r = CatalogoRemoto("http://127.0.0.1:8799/__interno"); f = Fetcher()
    try:
        out = await Lotto(r, f, SRC).esegui(modo, limite)
    finally:
        await f.aclose(); await r.aclose()
    print(json.dumps(out, ensure_ascii=False, indent=1))
asyncio.run(main())
