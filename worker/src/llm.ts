/**
 * Chiamate a Claude via HTTP (Messages API), senza SDK: nel Worker conta il
 * peso del bundle, e servono solo due operazioni.
 *
 * ANTHROPIC_BASE_URL permette di passare da AI Gateway di Cloudflare
 * (https://gateway.ai.cloudflare.com/v1/<account>/<gateway>/anthropic), che
 * aggiunge log, cache e un tetto di spesa in dollari.
 */
export interface LlmEnv {
  ANTHROPIC_API_KEY: string;
  ANTHROPIC_BASE_URL: string;
  LLM_MAX_TOKENS: string;
}

export interface LlmCall {
  model: string;
  system: string;
  user: string;
  maxTokens?: number;
}

// Temperatura zero: nella documentazione amministrativa la creativita' e' un
// difetto. Se il modello configurato non accetta il parametro, la prima
// richiesta fallisce con 400: si ripete senza e lo si ricorda per le successive.
let temperaturaAccettata = true;

async function post(env: LlmEnv, c: LlmCall, stream: boolean): Promise<Response> {
  const body = (withTemp: boolean) =>
    JSON.stringify({
      model: c.model,
      max_tokens: c.maxTokens ?? (Number(env.LLM_MAX_TOKENS) || 2000),
      system: c.system,
      messages: [{ role: "user", content: c.user }],
      stream,
      ...(withTemp ? { temperature: 0 } : {}),
    });
  const send = (withTemp: boolean) =>
    fetch(`${env.ANTHROPIC_BASE_URL.replace(/\/$/, "")}/v1/messages`, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "x-api-key": env.ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
      },
      body: body(withTemp),
    });

  let r = await send(temperaturaAccettata);
  if (r.status === 400 && temperaturaAccettata) {
    const txt = await r.clone().text();
    if (/temperature/i.test(txt)) {
      temperaturaAccettata = false;
      r = await send(false);
    }
  }
  if (!r.ok) {
    const txt = (await r.text()).slice(0, 300);
    throw new Error(`Claude API ${r.status}: ${txt}`);
  }
  return r;
}

export async function complete(env: LlmEnv, c: LlmCall): Promise<string> {
  const r = await post(env, c, false);
  const j = (await r.json()) as { content: { type: string; text?: string }[] };
  return j.content.filter((b) => b.type === "text").map((b) => b.text ?? "").join("");
}

/** Testo in streaming, dagli eventi SSE content_block_delta. */
export async function* stream(env: LlmEnv, c: LlmCall): AsyncGenerator<string> {
  const r = await post(env, c, true);
  const reader = r.body!.getReader();
  const dec = new TextDecoder();
  let buf = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let i: number;
    while ((i = buf.indexOf("\n\n")) >= 0) {
      const ev = buf.slice(0, i);
      buf = buf.slice(i + 2);
      const data = ev.split("\n").find((l) => l.startsWith("data: "));
      if (!data) continue;
      let j: any;
      try { j = JSON.parse(data.slice(6)); } catch { continue; }
      if (j.type === "content_block_delta" && j.delta?.type === "text_delta") yield j.delta.text as string;
      if (j.type === "error") throw new Error(`Claude API: ${j.error?.message ?? "errore in streaming"}`);
    }
  }
}
