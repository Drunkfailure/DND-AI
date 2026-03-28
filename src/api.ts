/** Base URL for API calls. Empty = same origin (production build served by FastAPI). Vite dev uses proxy. */
export function apiBase(): string {
  return import.meta.env.VITE_API_BASE ?? "";
}

export async function apiJson<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(`${apiBase()}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers as Record<string, string>),
    },
  });
  if (!r.ok) {
    const t = await r.text();
    throw new Error(t || `${r.status}`);
  }
  return r.json() as Promise<T>;
}

export async function apiNdjsonStream(
  path: string,
  body: unknown,
  onChunk: (s: string) => void,
): Promise<void> {
  const r = await fetch(`${apiBase()}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!r.ok) {
    const t = await r.text();
    throw new Error(t || `${r.status}`);
  }
  const reader = r.body?.getReader();
  if (!reader) throw new Error("no response body");
  const dec = new TextDecoder();
  let buf = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    const lines = buf.split("\n");
    buf = lines.pop() ?? "";
    for (const line of lines) {
      const s = line.trim();
      if (!s) continue;
      let j: { chunk?: string; error?: string; done?: boolean };
      try {
        j = JSON.parse(s) as { chunk?: string; error?: string; done?: boolean };
      } catch {
        continue;
      }
      if (j.error) throw new Error(j.error);
      if (j.chunk) onChunk(j.chunk);
    }
  }
  const tail = buf.trim();
  if (tail) {
    try {
      const j = JSON.parse(tail) as { chunk?: string; error?: string };
      if (j.error) throw new Error(j.error);
      if (j.chunk) onChunk(j.chunk);
    } catch (e) {
      if (e instanceof SyntaxError) return;
      throw e;
    }
  }
}
