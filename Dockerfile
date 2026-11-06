FROM python:3.11-slim

# tesseract: OCR dei PDF scansionati (delibere storiche, moduli firmati)
# antiword/catdoc/libreoffice: i .doc binari ancora presenti sul portale
# tzdata: senza, il container vive in UTC e lo scheduler parte all'ora sbagliata
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr tesseract-ocr-ita \
        antiword catdoc \
        libreoffice-writer-nogui \
        tzdata curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt pyproject.toml ./
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
RUN pip install --no-cache-dir -e .

# Il processo apre allegati arbitrari scaricati da internet con PyMuPDF,
# LibreOffice, antiword e tesseract, e tiene in ambiente una chiave API:
# non c'e' ragione perche' giri come root.
RUN useradd --uid 10001 --create-home fler \
    && mkdir -p /data && chown -R fler:fler /data /app
USER fler

ENV PYTHONUNBUFFERED=1 FLER_DATA_DIR=/data TZ=Europe/Rome
VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
  CMD curl -fsS http://localhost:8080/salute || exit 1

# --proxy-headers: dietro Caddy tutte le richieste arrivano dallo stesso
# indirizzo (il gateway della rete Docker). Senza leggere X-Forwarded-For il
# limite "30 domande l'ora per indirizzo" valeva per l'INTERO ufficio, e cinque
# password sbagliate di un collega bloccavano il login a tutti. La porta e'
# pubblicata solo su 127.0.0.1, quindi l'intestazione arriva solo dal proxy.
CMD ["uvicorn", "fler.api:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers", "--forwarded-allow-ips", "*"]
