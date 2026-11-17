import { readFileSync } from "node:fs";
import { setTimeout as delay } from "node:timers/promises";
import { Fault, need } from "./errors.mjs";
const paths = new Set([
  "/x/web-interface/view",
  "/x/web-interface/nav",
  "/x/web-interface/view/conclusion/get",
  "/x/player/wbi/v2",
  "/x/web-interface/popular",
  "/x/player/playurl",
]);
export function permitted(raw, kind = "api") {
  const u = new URL(raw);
  need(
    u.protocol === "https:" &&
      !u.username &&
      !u.password &&
      (!u.port || u.port === "443"),
    "URL_DENIED",
  );
  if (kind === "api")
    need(
      u.hostname === "api.bilibili.com" && paths.has(u.pathname),
      "URL_DENIED",
    );
  else if (kind === "subtitle")
    need(
      (u.hostname === "aisubtitle.hdslb.com" ||
        u.hostname === "i0.hdslb.com") &&
        (/\.(json)$/.test(u.pathname) ||
          /^\/bfs\/ai_subtitle\//.test(u.pathname)),
      "URL_DENIED",
    );
  else if (kind === "media")
    need(/(^|\.)bilivideo\.com$/.test(u.hostname), "URL_DENIED");
  else throw new Fault("URL_DENIED");
  return u;
}
export function credential(path) {
  if (!path) return null;
  try {
    const j = JSON.parse(readFileSync(path, "utf8"));
    need(j.disabled !== true && j.read_valid !== false, "CREDENTIAL_DISABLED");
    need(
      typeof j.sessdata === "string" &&
        j.sessdata.length > 0 &&
        j.sessdata.length < 4096 &&
        !/[\r\n;]/.test(j.sessdata),
      "CREDENTIAL_INVALID",
    );
    return j.sessdata;
  } catch {
    throw new Fault("CREDENTIAL_INVALID");
  }
}
// Inspect only bounded own-data causes; never copy error text or invoke getters.
function connectionReset(error) {
  try {
    const seen = new Set();
    let found = false;
    while (error && typeof error === "object" && !seen.has(error) && seen.size < 8) {
      seen.add(error);
      const fields = ["code", "name", "cause"].map((key) =>
        Object.getOwnPropertyDescriptor(error, key),
      );
      if (fields.some((field) => field && !Object.hasOwn(field, "value")))
        return false;
      const [code, name, cause] = fields.map((field) => field?.value);
      if (name === "AbortError" || name === "TimeoutError") return false;
      if (code !== undefined && code !== "ECONNRESET") return false;
      if (code === "ECONNRESET") found = true;
      error = cause;
    }
    return found && !error;
  } catch {
    return false;
  }
}
export class Network {
  constructor(
    store,
    budget,
    { credentialPath = "", fetcher = globalThis.fetch } = {},
  ) {
    this.store = store;
    this.budget = budget;
    try {
      this.secret = credential(credentialPath);
    } catch (e) {
      store.pause("CREDENTIAL_INVALID");
      throw e;
    }
    this.fetcher = fetcher;
    this.readViewReconnectsRemaining = 0;
  }
  async get(raw, kind = "api") {
    const u = permitted(raw, kind);
    return this.request(u, kind);
  }
  // Internal transport. Public tools receive typed operations, never URLs/options.
  async request(
    u,
    kind,
    { method = "GET", body, csrf = "", device = "" } = {},
  ) {
    if (method === "POST") {
      need(
        kind === "api" &&
          u.origin === "https://api.bilibili.com" &&
          !u.username &&
          !u.password &&
          !u.search &&
          [
            "/x/v2/history/report",
            "/x/v3/fav/resource/deal",
            "/x/web-interface/archive/like",
            "/x/web-interface/coin/add",
          ].includes(u.pathname) &&
          typeof this.beforePost === "function",
        "URL_DENIED",
      );
    } else {
      need(method === "GET", "URL_DENIED");
      if (
        !(
          kind === "api" &&
          u.origin === "https://api.bilibili.com" &&
          !u.username &&
          !u.password &&
          [
            "/x/v3/fav/folder/created/list-all",
            "/x/web-interface/history/cursor",
            "/x/web-interface/archive/has/like",
            "/x/web-interface/archive/coins",
          ].includes(u.pathname) &&
          typeof this.guard === "function"
        )
      )
        permitted(u.href, kind);
    }
    const headers = {
      "User-Agent": "dsh-bili-humanism/1.3.3",
      Referer: "https://www.bilibili.com/",
    };
    if (kind === "api" && this.secret)
      headers.Cookie =
        "SESSDATA=" +
        this.secret +
        (csrf ? "; bili_jct=" + csrf : "") +
        (device ? "; buvid3=" + device : "");
    if (body) headers["Content-Type"] = "application/x-www-form-urlencoded";
    let response;
    for (;;) {
      this.budget.spend("requests");
      const timeout = AbortSignal.timeout(
        Math.min(15000, this.budget.remaining()),
      );
      const signal = AbortSignal.any([
        timeout,
        ...(this.budget.signal ? [this.budget.signal] : []),
      ]);
      this.guard?.();
      if (method === "POST") this.beforePost?.();
      try {
        response = await this.fetcher(u, {
          method,
          ...(body ? { body: body.toString() } : {}),
          redirect: "manual",
          headers,
          signal,
        });
        break;
      } catch (error) {
        this.budget.check();
        const reconnect = this.readViewReconnectsRemaining === 1 &&
          method === "GET" && body === undefined && kind === "api" &&
          u.origin === "https://api.bilibili.com" &&
          u.pathname === "/x/web-interface/view" &&
          !signal.aborted && connectionReset(error) &&
          this.budget.requests < this.budget.limits.requests &&
          this.budget.remaining() > 750;
        if (!reconnect) throw new Fault("NETWORK_FAILED");
        this.readViewReconnectsRemaining = 0;
        this.guard?.();
        try {
          await delay(750, undefined, {
            signal: AbortSignal.any([
              AbortSignal.timeout(this.budget.remaining()),
              ...(this.budget.signal ? [this.budget.signal] : []),
            ]),
          });
        } catch {
          this.budget.check();
          throw new Fault("NETWORK_FAILED");
        }
        // The next attempt repeats budget, pause/cancel and guard checks.
      }
    }
    this.budget.check();
    if ([401, 403, 412, 429].includes(response.status)) {
      this.store.pause("HTTP_" + response.status);
      throw new Fault("PLATFORM_PAUSED");
    }
    need(response.status === 200, "HTTP_FAILED");
    const cap = kind === "media" ? 32 * 1024 * 1024 : 4 * 1024 * 1024;
    need(
      Number(response.headers.get("content-length") || 0) <= cap,
      "RESPONSE_TOO_LARGE",
    );
    const chunks = [];
    let length = 0;
    try {
      for await (const part of response.body) {
        this.budget.check();
        length += part.length;
        if (length > cap) {
          await response.body.cancel().catch(() => {});
          throw new Fault("RESPONSE_TOO_LARGE");
        }
        chunks.push(part);
      }
    } catch (e) {
      if (e instanceof Fault) throw e;
      this.budget.check();
      throw new Fault("BODY_READ_FAILED");
    }
    const data = Buffer.concat(chunks);
    if (kind === "media") return data;
    let j;
    try {
      j = JSON.parse(data.toString("utf8"));
    } catch {
      if (
        /captcha|验证码|验证页面/i.test(data.toString("utf8").slice(0, 8192))
      ) {
        this.store.pause("NON_JSON_CHALLENGE");
        throw new Fault("PLATFORM_PAUSED");
      }
      throw new Fault("INVALID_RESPONSE");
    }
    if (
      kind === "api" &&
      (j.data?.v_voucher ||
        /captcha|验证码|风控|账号异常/i.test(String(j.message || "")))
    ) {
      this.store.pause("API_CHALLENGE");
      throw new Fault("PLATFORM_PAUSED");
    }
    if (kind === "api" && j.code !== 0) {
      if ([-101, -111, -352, -412, -509, -403].includes(j.code)) {
        this.store.pause("API_" + j.code);
        throw new Fault(
          j.code === -101 ? "CREDENTIAL_EXPIRED" : "PLATFORM_PAUSED",
        );
      }
      throw new Fault("API_UNAVAILABLE");
    }
    return j;
  }
}
