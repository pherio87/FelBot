/**
 * Il container del crawler.
 *
 * Una sola istanza ("unico"). Il Worker la sveglia con un'attivita'
 * programmata e le chiede di eseguire un lotto: POST /lotto. Il container
 * (Python, Dockerfile.crawler) scarica, estrae e invia i risultati a
 * http://felbot.interno/... — richieste che non escono su internet: le
 * intercetta questa classe e le passa a gestisciInterno(), dentro il Worker.
 *
 * Verso il resto di internet il container esce normalmente: deve raggiungere
 * il portale della Regione.
 */
import { Container } from "@cloudflare/containers";
import { gestisciInterno, type InternoEnv } from "./interno";

export class Crawler extends Container<InternoEnv> {
  defaultPort = 8080;
  // Il lotto gira in background nel container e non genera richieste verso
  // la classe: senza attenzioni, dopo sleepAfter il container verrebbe
  // fermato a meta' lavoro. Vedi onActivityExpired.
  sleepAfter = "15m";
  enableInternet = true;

  static outboundByHost = {
    "felbot.interno": (req: Request, env: InternoEnv) => gestisciInterno(req, env),
  };

  /** Prima di spegnersi chiede al container se sta ancora lavorando. */
  override async onActivityExpired(): Promise<void> {
    try {
      const r = await this.containerFetch("http://container/stato");
      const s = (await r.json()) as { occupato?: boolean };
      if (s.occupato) {
        await this.renewActivityTimeout();
        return;
      }
    } catch {
      // container che non risponde: si spegne
    }
    await this.stop();
  }
}
