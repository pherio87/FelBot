"""Estrazione del testo dai formati in cui la Regione pubblica.

Principio guida: non perdere l'ancoraggio. Ogni frammento di testo si porta
dietro la pagina (nei PDF) o il titolo di sezione (negli altri formati), cosi'
la risposta finale puo' citare "pag. 7" e non solo "quel documento".
"""
from __future__ import annotations

import csv
import io
import logging
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

# Soglia sotto la quale un PDF si considera "scansionato" e si tenta l'OCR
OCR_MIN_CHARS_PER_PAGE = 90


@dataclass
class Block:
    text: str
    page: int | None = None
    heading: str | None = None


@dataclass
class Extraction:
    blocks: list[Block] = field(default_factory=list)
    n_pages: int | None = None
    method: str = ""
    warnings: list[str] = field(default_factory=list)
    # Questi due flag non sono decorativi: viaggiano fino al fascicolo di
    # estratti, dove l'avviso "testo da OCR" deve comparire per costruzione e
    # non per buona volonta' del modello.
    via_ocr: bool = False
    truncated: bool = False

    @property
    def text(self) -> str:
        return "\n\n".join(b.text for b in self.blocks)

    @property
    def n_chars(self) -> int:
        return sum(len(b.text) for b in self.blocks)


_WS = re.compile(r"[ \t\u00a0]+")
_NL = re.compile(r"\n{3,}")
# sillabazione a fine riga tipica dei PDF impaginati
_HYPH = re.compile(r"(\w)-\n(\w)")


def clean(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _HYPH.sub(r"\1\2", text)
    text = _WS.sub(" ", text)
    text = _NL.sub("\n\n", text)
    return text.strip()


# --------------------------------------------------------------------- PDF
def extract_pdf(data: bytes, allow_ocr: bool = True, ocr_max_pages: int = 120) -> Extraction:
    """Estrae il testo di un PDF pagina per pagina.

    Due accorgimenti appresi a caro prezzo:

    - `sort=True`: senza ordinamento PyMuPDF restituisce i blocchi nell'ordine
      in cui stanno nel file, non in ordine di lettura. Sui documenti a due
      colonne (i bollettini regionali) le righe delle colonne si interlacciano
      e producono testo sintatticamente inservibile che supera comunque i
      controlli di lunghezza e finisce a indice come valido.

    - La decisione sull'OCR e' PER PAGINA, non sulla media del documento. Una
      delibera di 200 pagine con 50 pagine di allegato scansionato ha una media
      alta e non attiverebbe mai l'OCR: quelle 50 pagine si perderebbero in
      silenzio.
    """
    import fitz  # PyMuPDF

    out = Extraction(method="pymupdf")
    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception as exc:
        out.warnings.append(f"PDF illeggibile: {exc}")
        return out

    if doc.is_encrypted and not doc.authenticate(""):
        out.warnings.append("PDF protetto da password")
        doc.close()
        return out

    out.n_pages = doc.page_count
    texts: dict[int, str] = {}
    need_ocr: list[int] = []
    for i, page in enumerate(doc, start=1):
        try:
            t = clean(page.get_text("text", sort=True) or "")
        except Exception:
            t = ""
        texts[i] = t
        if len(t) < OCR_MIN_CHARS_PER_PAGE:
            need_ocr.append(i)

    if allow_ocr and need_ocr:
        if len(need_ocr) > ocr_max_pages:
            out.truncated = True
            out.warnings.append(
                f"OCR limitato alle prime {ocr_max_pages} pagine su "
                f"{len(need_ocr)} da riconoscere: il documento e' indicizzato in modo parziale."
            )
            need_ocr = need_ocr[:ocr_max_pages]
        recovered = _ocr_pages(doc, need_ocr)
        for page_no, t in recovered.items():
            if len(t) > len(texts.get(page_no, "")):
                texts[page_no] = t
                out.via_ocr = True
        if out.via_ocr:
            out.warnings.append(
                "Parte del testo e' stata ottenuta via OCR: possibili imprecisioni "
                "di trascrizione, in particolare sulle cifre."
            )

    for i in sorted(texts):
        if texts[i]:
            out.blocks.append(Block(text=texts[i], page=i))
    doc.close()
    return out


def _ocr_pages(doc, pages: list[int]) -> dict[int, str]:
    """OCR delle sole pagine indicate (delibere scansionate, moduli firmati)."""
    out: dict[int, str] = {}
    try:
        import pytesseract
        from PIL import Image
    except Exception as exc:
        log.warning("OCR non disponibile (%s): le pagine scansionate resteranno vuote", exc)
        return out

    for page_no in pages:
        try:
            pix = doc[page_no - 1].get_pixmap(dpi=220)
            img = Image.open(io.BytesIO(pix.tobytes("png")))
            t = clean(pytesseract.image_to_string(img, lang="ita+eng"))
            if t:
                out[page_no] = t
        except Exception as exc:
            log.debug("OCR pagina %s fallito: %s", page_no, exc)
    return out


# -------------------------------------------------------------------- DOCX
def extract_docx(data: bytes) -> Extraction:
    import docx  # python-docx

    out = Extraction(method="python-docx")
    try:
        d = docx.Document(io.BytesIO(data))
    except Exception as exc:
        out.warnings.append(f"DOCX illeggibile: {exc}")
        return out

    heading: str | None = None
    buf: list[str] = []

    def flush() -> None:
        if buf:
            t = clean("\n".join(buf))
            if t:
                out.blocks.append(Block(text=t, heading=heading))
            buf.clear()

    for p in d.paragraphs:
        txt = (p.text or "").strip()
        if not txt:
            continue
        style = (p.style.name or "").lower() if p.style else ""
        if style.startswith(("heading", "titolo")):
            flush()
            heading = txt
            buf.append(txt)
        else:
            buf.append(txt)
    flush()

    for ti, table in enumerate(d.tables, start=1):
        rows = []
        for row in table.rows:
            cells = [c.text.strip().replace("\n", " ") for c in row.cells]
            if any(cells):
                rows.append(" | ".join(cells))
        if rows:
            out.blocks.append(
                Block(text="Tabella %d:\n%s" % (ti, "\n".join(rows)), heading=heading)
            )
    return out


# ---------------------------------------------------------- DOC legacy (.doc)
def extract_doc_legacy(data: bytes) -> Extraction:
    """I .doc binari richiedono un convertitore esterno (antiword o libreoffice)."""
    out = Extraction(method="antiword")
    with tempfile.NamedTemporaryFile(suffix=".doc", delete=False) as fh:
        fh.write(data)
        tmp = fh.name
    try:
        for cmd, meth in (
            (["antiword", "-m", "UTF-8.txt", tmp], "antiword"),
            (["catdoc", "-d", "utf-8", tmp], "catdoc"),
        ):
            try:
                r = subprocess.run(cmd, capture_output=True, timeout=120)
                if r.returncode == 0 and r.stdout.strip():
                    out.method = meth
                    out.blocks = [Block(text=clean(r.stdout.decode("utf-8", "replace")))]
                    return out
            except (FileNotFoundError, subprocess.TimeoutExpired):
                continue
        # ultima spiaggia: LibreOffice headless
        txt = _soffice_to_text(tmp)
        if txt:
            out.method = "libreoffice"
            out.blocks = [Block(text=txt)]
            return out
        out.warnings.append(".doc non convertibile: installare antiword o libreoffice")
        return out
    finally:
        Path(tmp).unlink(missing_ok=True)


def _soffice_to_text(path: str, timeout: int = 240) -> str:
    """Conversione via LibreOffice headless, isolata e con un vero timeout.

    Due dettagli che sembrano pedanteria e non lo sono: senza
    `-env:UserInstallation` due conversioni concorrenti si contendono lo stesso
    profilo utente e la seconda muore in silenzio; e `subprocess.run(timeout=)`
    uccide il wrapper `soffice`, non il `soffice.bin` figlio, che resta orfano a
    mangiarsi la memoria del VPS per tutta la prima indicizzazione.
    """
    import os
    import signal

    with tempfile.TemporaryDirectory() as td:
        profile = Path(td) / "profile"
        cmd = [
            "soffice", f"-env:UserInstallation=file://{profile}",
            "--headless", "--norestore", "--invisible",
            "--convert-to", "txt:Text", "--outdir", td, path,
        ]
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except FileNotFoundError:
            return ""
        try:
            proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                proc.kill()
            proc.communicate()
            log.warning("conversione LibreOffice interrotta per timeout: %s", path)
            return ""
        for f in Path(td).glob("*.txt"):
            return clean(f.read_text("utf-8", "replace"))
    return ""


# -------------------------------------------------------------------- PPTX
def extract_pptx(data: bytes) -> Extraction:
    out = Extraction(method="python-pptx")
    try:
        from pptx import Presentation
    except Exception:
        # senza python-pptx si tenta LibreOffice, meglio che perdere il file
        with tempfile.NamedTemporaryFile(suffix=".pptx", delete=False) as fh:
            fh.write(data)
            tmp = fh.name
        try:
            t = _soffice_to_text(tmp)
            if t:
                out.method = "libreoffice"
                out.blocks = [Block(text=t)]
            else:
                out.warnings.append("PPTX non estraibile: installare python-pptx")
        finally:
            Path(tmp).unlink(missing_ok=True)
        return out

    try:
        prs = Presentation(io.BytesIO(data))
    except Exception as exc:
        out.warnings.append(f"PPTX illeggibile: {exc}")
        return out
    for i, slide in enumerate(prs.slides, start=1):
        parts: list[str] = []
        for shape in slide.shapes:
            if getattr(shape, "has_text_frame", False):
                t = shape.text_frame.text.strip()
                if t:
                    parts.append(t)
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame:
            n = slide.notes_slide.notes_text_frame.text.strip()
            if n:
                parts.append(f"Note: {n}")
        if parts:
            out.blocks.append(Block(text=clean("\n".join(parts)), page=i,
                                    heading=f"Diapositiva {i}"))
    return out


# --------------------------------------------------------------------- ZIP
# Il portale pubblica archivi di modulistica: senza questo ramo i PDF che
# contengono non entrano nell'indice e il documento chiude come "senza testo".
ZIP_MAX_MEMBERS = 60
ZIP_MAX_MEMBER_BYTES = 40 * 1024 * 1024


def extract_zip(data: bytes, _depth: int = 0) -> Extraction:
    import zipfile

    out = Extraction(method="zip")
    if _depth > 1:
        out.warnings.append("archivi annidati oltre il primo livello: ignorati")
        return out
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except Exception as exc:
        out.warnings.append(f"archivio illeggibile: {exc}")
        return out

    names = [n for n in zf.namelist() if not n.endswith("/")]
    if len(names) > ZIP_MAX_MEMBERS:
        out.truncated = True
        out.warnings.append(f"archivio con {len(names)} file: estratti i primi {ZIP_MAX_MEMBERS}")
        names = names[:ZIP_MAX_MEMBERS]

    for name in names:
        try:
            info = zf.getinfo(name)
            if info.file_size > ZIP_MAX_MEMBER_BYTES:
                out.warnings.append(f"'{name}' troppo grande: saltato")
                continue
            member = zf.read(name)
        except Exception as exc:
            out.warnings.append(f"'{name}' non estraibile: {exc}")
            continue
        kind = sniff(member, None, name)
        if kind in ("unknown", "zip") and _depth >= 1:
            continue
        sub = extract_zip(member, _depth + 1) if kind == "zip" else extract(member, filename=name)
        out.via_ocr = out.via_ocr or sub.via_ocr
        out.truncated = out.truncated or sub.truncated
        out.warnings.extend(f"[{name}] {w}" for w in sub.warnings)
        for b in sub.blocks:
            out.blocks.append(Block(text=b.text, page=b.page, heading=b.heading or name))
    if not out.blocks:
        out.warnings.append("nessun file leggibile nell'archivio")
    return out


# ------------------------------------------------------------- ODT / ODS / ODP
def extract_odf(data: bytes) -> Extraction:
    from odf import teletype, text as odftext
    from odf.opendocument import load

    out = Extraction(method="odfpy")
    try:
        doc = load(io.BytesIO(data))
    except Exception as exc:
        out.warnings.append(f"ODF illeggibile: {exc}")
        return out

    heading: str | None = None
    buf: list[str] = []
    for el in doc.text.childNodes if doc.text else []:
        name = getattr(el, "qname", (None, ""))[1]
        try:
            t = teletype.extractText(el).strip()
        except Exception:
            t = ""
        if not t:
            continue
        if name == "h":
            if buf:
                out.blocks.append(Block(text=clean("\n".join(buf)), heading=heading))
                buf = []
            heading = t
        buf.append(t)
    if buf:
        out.blocks.append(Block(text=clean("\n".join(buf)), heading=heading))

    if not out.blocks:  # fallback grezzo
        try:
            allp = doc.getElementsByType(odftext.P)
            t = clean("\n".join(teletype.extractText(p) for p in allp))
            if t:
                out.blocks = [Block(text=t)]
        except Exception:
            pass
    return out


# -------------------------------------------------------------- XLSX / XLS / CSV
def extract_xlsx(data: bytes) -> Extraction:
    import openpyxl

    out = Extraction(method="openpyxl")
    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:
        out.warnings.append(f"XLSX illeggibile: {exc}")
        return out
    for ws in wb.worksheets:
        rows = []
        for row in ws.iter_rows(values_only=True):
            cells = [str(c).strip() for c in row if c not in (None, "")]
            if cells:
                rows.append(" | ".join(cells))
            if len(rows) > 4000:
                out.warnings.append(f"Foglio '{ws.title}' troncato a 4000 righe")
                break
        if rows:
            out.blocks.append(
                Block(text="Foglio '%s':\n%s" % (ws.title, "\n".join(rows)), heading=ws.title)
            )
    wb.close()
    return out


def extract_csv(data: bytes) -> Extraction:
    out = Extraction(method="csv")
    txt = data.decode("utf-8", "replace")
    try:
        dialect = csv.Sniffer().sniff(txt[:4000])
    except Exception:
        dialect = csv.excel  # type: ignore[assignment]
    rows = [" | ".join(r) for r in csv.reader(io.StringIO(txt), dialect) if any(r)]
    if rows:
        out.blocks = [Block(text="\n".join(rows[:5000]))]
    return out


# -------------------------------------------------------------------- HTML
_DROP = (
    "script", "style", "noscript", "nav", "footer", "header", "aside",
    "form", "iframe", "svg", "button",
)
_MAIN = (
    "#content-core", "#content", "main", "article", "[role=main]",
    ".entry-content", ".content-core", "#portal-column-content",
)


def extract_html(data: bytes | str) -> Extraction:
    from selectolax.parser import HTMLParser

    out = Extraction(method="selectolax")
    html = data.decode("utf-8", "replace") if isinstance(data, bytes) else data
    tree = HTMLParser(html)
    for sel in _DROP:
        for node in tree.css(sel):
            node.decompose()
    # rimuove elementi tipici di Plone/Volto che sporcano il testo
    for sel in (".skiplinks", "#portal-breadcrumbs", ".cookie", "#portal-footer",
                ".searchSection", ".menu-wrapper", ".it-header-wrapper"):
        for node in tree.css(sel):
            node.decompose()

    root = None
    for sel in _MAIN:
        root = tree.css_first(sel)
        if root is not None:
            break
    root = root or tree.body or tree.root
    if root is None:
        return out

    heading: str | None = None
    buf: list[str] = []
    for node in root.traverse(include_text=False):
        tag = node.tag
        if tag in ("h1", "h2", "h3", "h4"):
            t = node.text(strip=True)
            if not t:
                continue
            if buf:
                out.blocks.append(Block(text=clean("\n".join(buf)), heading=heading))
                buf = []
            heading = t
            buf.append(t)
        elif tag in ("p", "li", "td", "th", "dd", "dt", "blockquote", "pre"):
            t = node.text(strip=True, separator=" ")
            if t and len(t) > 1:
                buf.append(t)
    if buf:
        out.blocks.append(Block(text=clean("\n".join(buf)), heading=heading))

    if not out.blocks:
        t = clean(root.text(separator="\n", strip=True))
        if t:
            out.blocks = [Block(text=t)]
    # deduplica blocchi identici consecutivi (menu ripetuti)
    dedup: list[Block] = []
    for b in out.blocks:
        if dedup and dedup[-1].text == b.text:
            continue
        dedup.append(b)
    out.blocks = dedup
    return out


def extract_txt(data: bytes) -> Extraction:
    out = Extraction(method="plain")
    t = clean(data.decode("utf-8", "replace"))
    if t:
        out.blocks = [Block(text=t)]
    return out


def extract_rtf(data: bytes) -> Extraction:
    from striprtf.striprtf import rtf_to_text

    out = Extraction(method="striprtf")
    try:
        t = clean(rtf_to_text(data.decode("utf-8", "replace"), errors="ignore"))
        if t:
            out.blocks = [Block(text=t)]
    except Exception as exc:
        out.warnings.append(f"RTF illeggibile: {exc}")
    return out


# ------------------------------------------------------------------ dispatch
def sniff(data: bytes, mime: str | None, filename: str | None) -> str:
    head = data[:8]
    if head[:4] == b"%PDF":
        return "pdf"
    if head[:2] == b"PK":  # contenitore OOXML/ODF
        low = (filename or "").lower()
        if low.endswith((".docx", ".dotx")):
            return "docx"
        if low.endswith((".xlsx", ".xlsm")):
            return "xlsx"
        if low.endswith((".odt", ".ods", ".odp")):
            return "odf"
        head_zip = data[:8000]
        if low.endswith((".pptx", ".potx")) or b"ppt/" in head_zip:
            return "pptx"
        if b"word/" in head_zip:
            return "docx"
        if b"xl/" in head_zip:
            return "xlsx"
        if b"opendocument" in head_zip:
            return "odf"
        # Un contenitore ZIP non riconosciuto NON e' un docx: il portale
        # pubblica archivi di modulistica, e trattarli come Word li faceva
        # fallire in silenzio chiudendoli come "senza testo".
        return "zip"
    if head[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":  # OLE2: .doc/.xls storici
        return "xls" if (filename or "").lower().endswith((".xls", ".xlt")) else "doc"
    if head[:5] == b"{\\rtf":
        return "rtf"

    m = (mime or "").lower()
    f = (filename or "").lower()
    if "pdf" in m or f.endswith(".pdf"):
        return "pdf"
    if "wordprocessingml" in m or f.endswith(".docx"):
        return "docx"
    if "msword" in m or f.endswith(".doc"):
        return "doc"
    if "opendocument" in m or f.endswith((".odt", ".ods", ".odp")):
        return "odf"
    if "spreadsheetml" in m or f.endswith((".xlsx", ".xlsm")):
        return "xlsx"
    if "ms-excel" in m or f.endswith(".xls"):
        return "xls"
    if "presentationml" in m or f.endswith((".pptx", ".potx")):
        return "pptx"
    if "zip" in m or f.endswith(".zip"):
        return "zip"
    if "csv" in m or f.endswith(".csv"):
        return "csv"
    if "rtf" in m or f.endswith(".rtf"):
        return "rtf"
    if "html" in m or f.endswith((".html", ".htm")):
        return "html"
    if "text/plain" in m or f.endswith(".txt"):
        return "txt"
    return "html" if b"<html" in data[:2000].lower() else "unknown"


def extract(data: bytes, mime: str | None = None, filename: str | None = None) -> Extraction:
    kind = sniff(data, mime, filename)
    try:
        if kind == "pdf":
            return extract_pdf(data)
        if kind == "docx":
            return extract_docx(data)
        if kind == "doc":
            return extract_doc_legacy(data)
        if kind == "odf":
            return extract_odf(data)
        if kind == "xlsx":
            return extract_xlsx(data)
        if kind == "xls":
            return _extract_xls(data)
        if kind == "csv":
            return extract_csv(data)
        if kind == "rtf":
            return extract_rtf(data)
        if kind == "txt":
            return extract_txt(data)
        if kind == "pptx":
            return extract_pptx(data)
        if kind == "zip":
            return extract_zip(data)
        if kind == "html":
            return extract_html(data)
    except Exception as exc:  # nessun documento deve far cadere la pipeline
        log.warning("estrazione fallita (%s): %s", kind, exc)
        e = Extraction(method=kind)
        e.warnings.append(f"estrazione fallita: {exc}")
        return e
    e = Extraction(method="unknown")
    e.warnings.append(f"formato non riconosciuto (mime={mime}, file={filename})")
    return e


def _extract_xls(data: bytes) -> Extraction:
    import xlrd

    out = Extraction(method="xlrd")
    try:
        wb = xlrd.open_workbook(file_contents=data)
    except Exception as exc:
        out.warnings.append(f"XLS illeggibile: {exc}")
        return out
    for sh in wb.sheets():
        rows = []
        for r in range(min(sh.nrows, 4000)):
            cells = [str(c.value).strip() for c in sh.row(r) if str(c.value).strip()]
            if cells:
                rows.append(" | ".join(cells))
        if rows:
            out.blocks.append(
                Block(text="Foglio '%s':\n%s" % (sh.name, "\n".join(rows)), heading=sh.name)
            )
    return out
