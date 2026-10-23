export interface JsonResult<T> {
  status: number;
  ok: boolean;
  body: T | null;
}

/**
 * 请求同源 `/api` 接口。非 2xx 响应也可能带有可展示的 JSON 正文
 * （例如 ready 的 503），因此只有网络错误或非 JSON 响应才抛出。
 */
export async function getJson<T>(
  path: string,
  signal: AbortSignal,
): Promise<JsonResult<T>> {
  const response = await fetch(path, {
    signal,
    cache: "no-store",
    headers: { Accept: "application/json" },
  });
  const text = await response.text();
  let body: T | null = null;
  if (text.length > 0) {
    try {
      body = JSON.parse(text) as T;
    } catch {
      body = null;
    }
  }
  return { status: response.status, ok: response.ok, body };
}

export async function postJson<T>(
  path: string,
  payload: unknown,
): Promise<JsonResult<T>> {
  const response = await fetch(path, {
    method: "POST",
    cache: "no-store",
    headers: { Accept: "application/json", "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const text = await response.text();
  let body: T | null = null;
  if (text.length > 0) {
    try {
      body = JSON.parse(text) as T;
    } catch {
      body = null;
    }
  }
  return { status: response.status, ok: response.ok, body };
}
