import json, sys
docs = [
 {"path": "accreditamento/dgr-1298-2015.txt", "uid": "u1", "type": "File", "title": "DGR 1298/2015 Accreditamento organismi di formazione", "modified": "2025-03-01",
  "text": "Delibera di Giunta regionale n. 1298 del 2015. Gli organismi di formazione accreditati trasmettono la comunicazione delle variazioni entro trenta giorni. Il contributo massimo per progetto e' pari a 50.000 euro."},
 {"path": "iefp/stage-iefp", "uid": "u2", "type": "Document", "title": "Stage nei percorsi IeFP", "modified": "2025-04-01",
  "text": "Nei percorsi di Istruzione e Formazione Professionale lo stage curricolare ha una durata minima di 300 ore nel secondo anno e di 400 ore nel terzo anno."},
 {"path": "bandi/graduatoria-oss.txt", "uid": "u3", "type": "File", "title": "Graduatoria finale corso OSS 2025", "modified": "2025-05-01", "text": "ROSSI MARIO 98 punti"},
 {"path": "bandi/allegato-1.txt", "uid": "u4", "type": "File", "title": "Allegato 1", "modified": "2025-05-02",
  "text": "Elenco: ROSSI MARIO RSSMRA80A01A944A; BIANCHI LUCA BNCLCU81B02A944B; VERDI ANNA VRDNNA82C43A944C; documento dell'operazione approvata."},
 {"path": "vari/vuoto.txt", "uid": "u5", "type": "File", "title": "Nota breve", "modified": "2025-05-03", "text": "ok"},
 {"path": "vari/rotto.pdf", "uid": "u6", "type": "File", "title": "Allegato non scaricabile", "modified": "2025-05-04", "text": "", "download_404": True},
]
for i in range(44):
    docs.append({"path": f"archivio/atto-{i}.txt", "uid": f"f{i}", "type": "File", "title": f"Determinazione dirigenziale n. {1000+i} del 2024",
                 "modified": "2024-06-01", "text": f"Determinazione dirigenziale n. {1000+i} del 2024 relativa all'approvazione delle operazioni formative del lotto {i}, con durata di {100+i} ore."})
for d in docs: d.update(listed=True, exists=True)
json.dump({"docs": docs}, open(sys.argv[1], "w"), indent=1)
print(len(docs), "documenti")
