import { join } from "node:path";
import { homedir } from "node:os";
import { mkdirSync, readFileSync } from "node:fs";
import { spawn } from "node:child_process";
import { randomUUID } from "node:crypto";
import { fileURLToPath } from "node:url";
import { Store, atomic } from "./store.mjs";
import { Budget } from "./budget.mjs";
import { Network } from "./network.mjs";
import { readVideo, candidates, coverage, segments } from "./reader.mjs";
import { need, safeError } from "./errors.mjs";
import { Writer, grant, writeStatus, confirmConsumption } from "./writer.mjs";
import { personality } from "./personality.mjs";
import { fields, exportInbox, readInbox, verifyInbox } from "./inbox.mjs";
import {
  enforceJournalDailyLimit,
  journalDailyStatus,
  evidenceFingerprint,
} from "./journal-policy.mjs";
const defaultLimits = {
  requests: 12,
  candidates: 3,
  asrSeconds: 600,
  wallMs: 90000,
};
export function text(value, max = 12000) {
  need(typeof value === "string" && value.length <= max, "INVALID_TEXT");
  return value.trim();
}
export function noteBody(raw, reading) {
  need(raw && typeof raw === "object" && !Array.isArray(raw), "INVALID_NOTE");
  need(
    Object.keys(raw).every((k) => fields.includes(k) || k === "evidence"),
    "INVALID_NOTE_FIELD",
  );
  const body = Object.fromEntries(fields.map((k) => [k, text(raw[k] || "")]));
  need(body.encounter && body.missing, "NOTE_REQUIRES_ENCOUNTER_AND_LIMITS");
  need(
    Array.isArray(raw.evidence) && raw.evidence.length <= 20,
    "INVALID_EVIDENCE",
  );
  body.evidence = raw.evidence.map((e) => {
    need(
      Number.isFinite(e.at) && e.at >= 0 && e.at <= reading.duration,
      "INVALID_EVIDENCE_TIME",
    );
    return {
      at: e.at,
      claim: text(e.claim, 2000),
      url: reading.url + "&t=" + Math.floor(e.at),
    };
  });
  if (body.facts || body.author_view)
    need(body.evidence.length > 0, "EVIDENCE_REQUIRED");
  return body;
}
export class Service {
  constructor(config = {}, dependencies = {}) {
    this.config = {
      ...config,
      interestDriven: config.interestDriven === true,
      enablePersonalityLearning:
        config.enablePersonalityLearning === undefined
          ? true
          : config.enablePersonalityLearning,
    };
    this.store = new Store(
      config.dataDir ||
        join(process.env.DSH_HOME || join(homedir(), ".dsh"), "bili-humanism"),
    );
    this.limits = { ...defaultLimits, ...config.limits };
    for (const key of Object.keys(defaultLimits))
      need(
        Number.isFinite(this.limits[key]) &&
          this.limits[key] >= 0 &&
          this.limits[key] <= defaultLimits[key],
        "INVALID_LIMIT",
      );
    this.fetcher = dependencies.fetcher || globalThis.fetch;
    this.active = new Set();
  }
  async task(fn, signal) {
    const id = this.store.start();
    const controller = new AbortController();
    this.active.add(controller);
    const budget = new Budget(
      this.store,
      id,
      this.limits,
      signal ? AbortSignal.any([signal, controller.signal]) : controller.signal,
    );
    try {
      const network = new Network(this.store, budget, {
        credentialPath: this.config.credentialPath,
        fetcher: this.fetcher,
      });
      const result = await fn(network, budget);
      budget.check();
      this.store.finish(id, "completed");
      return {
        taskId: id,
        result,
        usage: {
          requests: budget.requests,
          candidates: budget.candidates,
          asr_seconds: budget.asr,
          elapsed_ms: Date.now() - budget.started,
          tokens: null,
        },
      };
    } catch (e) {
      this.store.finish(
        id,
        e.code === "CANCELLED" ? "cancelled" : "failed",
        e.code || "INTERNAL_ERROR",
      );
      throw e;
    } finally {
      this.active.delete(controller);
      if (this.closing && this.active.size === 0) this.store.close();
    }
  }
  async read(input, page, signal) {
    return this.task(async (network) => {
      const reading = await readVideo(input, page, network, this.store);
      return this.store.saveReading(reading);
    }, signal);
  }
  async candidates(count = 3, signal) {
    return this.task((network) => candidates(network, count), signal);
  }
  status() {
    const writes = writeStatus(this.config);
    return {
      ...this.store.status(),
      journals: journalDailyStatus(
        this.store,
        Date.now(),
        this.config.interestDriven,
      ),
      platformWriteTools: writes.platformWriteTools,
      writes,
    };
  }
  async platformWrite(kind, args, signal) {
    need(
      ["history", "favorites", "likes", "coins"].includes(kind),
      "WRITE_SCOPE_DENIED",
    );
    grant(this.config, kind);
    return this.task(
      (network) => new Writer(this.config, this.store, network).run(kind, args),
      signal,
    );
  }
  confirmConsumption(readingId, seconds) {
    return confirmConsumption(this.store, readingId, seconds);
  }
  personality(action, args = {}) {
    return personality(
      this.store,
      action,
      args,
      this.config.enablePersonalityLearning === true,
      this.config.interestDriven,
    );
  }
  journal(readingId, raw) {
    const reading = this.store.reading(readingId);
    const body = noteBody(raw, reading);
    return this.store.tx(() => {
      const existing = this.store.db
        .prepare(
          "SELECT id FROM notes WHERE reading_id=? AND body=? ORDER BY created,id LIMIT 1",
        )
        .get(readingId, JSON.stringify(body));
      const evidenceHash = evidenceFingerprint(body.evidence);
      const sameEvidence =
        this.config.interestDriven && evidenceHash && !existing
          ? this.store.db
              .prepare(
                "SELECT id,body FROM notes WHERE reading_id=? ORDER BY created,id",
              )
              .all(readingId)
              .find(
                (row) =>
                  evidenceFingerprint(JSON.parse(row.body).evidence) ===
                  evidenceHash,
              )
          : null;
      if (existing || sameEvidence) {
        verifyInbox(this.store);
        return {
          id: (existing || sameEvidence).id,
          readingId,
          status: "local_reviewable_draft",
          path: join(this.store.root, "inbox.md"),
          deduplicated: true,
        };
      }
      const created = new Date().toISOString();
      enforceJournalDailyLimit(this.store, created, this.config.interestDriven);
      if (this.store.getControl("inbox:index-hash")) verifyInbox(this.store);
      const id = this.store.note(readingId, body, created);
      const path = exportInbox(this.store);
      return {
        id,
        readingId,
        status: "local_reviewable_draft",
        path,
        deduplicated: false,
      };
    });
  }
  inbox(args = {}) {
    return readInbox(this.store, args);
  }
  exportInbox() {
    return this.store.tx(() => exportInbox(this.store));
  }
  importLocal(readingId, raw) {
    return this.task(async (_network, budget) => {
      const r = this.store.reading(readingId);
      need(["A3", "frame"].includes(raw.route), "INVALID_ROUTE");
      if (raw.route === "A3") {
        need(raw.duration === r.duration, "DURATION_MISMATCH");
        budget.spend("asr", raw.duration);
      }
      const segs = segments(raw.segments, r.duration, raw.route);
      need(segs.length > 0, "EMPTY_LOCAL_SOURCE");
      return this.store.saveReading({
        ...r,
        route: raw.route,
        segments: segs,
        coverage: {
          ...coverage(segs, r.duration, raw.route),
          state: raw.route === "frame" ? "visual_text_partial" : "asr_partial",
        },
        localEvidence:
          "User/host supplied import; content not independently verified.",
        cached: false,
      });
    });
  }
  async audio(readingId, signal) {
    const reading = this.store.reading(readingId);
    return this.task(async (network, budget) => {
      need(reading.duration <= this.limits.asrSeconds, "ASR_BUDGET");
      const u = new URL("https://api.bilibili.com/x/player/playurl");
      u.search = new URLSearchParams({
        bvid: reading.bvid,
        cid: reading.cid,
        qn: 16,
        fnval: 16,
      });
      const response = await network.get(u.href);
      const tracks = response.data?.dash?.audio || [];
      const selected = [...tracks].sort((a, b) => a.bandwidth - b.bandwidth)[0];
      need(selected, "AUDIO_UNAVAILABLE");
      const bytes = await network.get(
        selected.baseUrl || selected.base_url,
        "media",
      );
      const dir = join(this.store.root, "assets");
      mkdirSync(dir, { recursive: true, mode: 0o700 });
      const path = join(dir, randomUUID() + ".m4s");
      atomic(path, bytes);
      return {
        path,
        duration: reading.duration,
        bytes: bytes.length,
        note: "Local study asset; not a transcript or a public deliverable.",
      };
    }, signal);
  }
  async localMedia(mode, readingId, input, model = "", signal) {
    const reading = this.store.reading(readingId);
    need(["asr", "frames"].includes(mode), "INVALID_ROUTE");
    return this.task(async (_network, budget) => {
      if (mode === "asr") budget.spend("asr", reading.duration);
      else
        need(
          reading.duration <= this.limits.asrSeconds,
          "MEDIA_DURATION_BUDGET",
        );
      const dir = join(this.store.root, "assets");
      mkdirSync(dir, { recursive: true, mode: 0o700 });
      const output = join(dir, randomUUID() + (mode === "asr" ? ".json" : ""));
      const argv = [
        fileURLToPath(new URL("../scripts/media.py", import.meta.url)),
        mode,
        input,
        output,
        "--duration",
        String(reading.duration),
        "--max-wall-seconds",
        String(Math.max(1, Math.floor(budget.remaining() / 1000))),
      ];
      if (mode === "asr") argv.push("--model", model);
      await new Promise((resolve, reject) => {
        const child = spawn(
          process.env.BILI_HUMANISM_PYTHON || "python",
          argv,
          { stdio: ["ignore", "ignore", "ignore"], windowsHide: true },
        );
        let fault = null;
        const stop = () => {
          fault = new Error("cancel");
          child.kill();
        };
        const tick = setInterval(() => {
          try {
            budget.check();
          } catch (e) {
            fault = e;
            child.kill();
          }
        }, 100);
        budget.signal?.addEventListener("abort", stop, { once: true });
        child.once("error", () => {
          clearInterval(tick);
          budget.signal?.removeEventListener("abort", stop);
          reject(new Error("Local media dependency failed"));
        });
        child.once("exit", (code) => {
          clearInterval(tick);
          budget.signal?.removeEventListener("abort", stop);
          try {
            budget.check();
            if (fault) throw fault;
            need(code === 0, "LOCAL_MEDIA_FAILED");
            resolve();
          } catch (e) {
            reject(e);
          }
        });
      });
      if (mode === "frames")
        return {
          path: output,
          status: "sampled_frames",
          note: "No automatic OCR. Host vision interpretation must retain timestamps and partial-coverage warning.",
        };
      const raw = JSON.parse(readFileSync(output, "utf8"));
      const segs = segments(raw.segments, reading.duration, "A3");
      need(segs.length, "EMPTY_LOCAL_SOURCE");
      return this.store.saveReading({
        ...reading,
        route: "A3",
        segments: segs,
        coverage: {
          ...coverage(segs, reading.duration, "A3"),
          state: "asr_partial",
        },
        localEvidence:
          "Local AI-ASR, not verbatim. Proper names require source checking.",
        cached: false,
      });
    }, signal);
  }
  memory(action, id = "", body = null) {
    need(["list", "put", "delete"].includes(action), "INVALID_MEMORY_ACTION");
    if (action === "put") {
      need(
        body && ["self", "user"].includes(body.subject),
        "THIRD_PARTY_PROFILE_DENIED",
      );
      need(
        Object.keys(body).every((k) =>
          [
            "subject",
            "understanding",
            "evidence",
            "uncertainty",
            "next_response",
          ].includes(k),
        ),
        "INVALID_MEMORY_FIELD",
      );
      for (const key of [
        "understanding",
        "evidence",
        "uncertainty",
        "next_response",
      ])
        text(body[key] || "");
      need(body.understanding && body.evidence, "MEMORY_EVIDENCE_REQUIRED");
    }
    return this.store.memory(action, id, body);
  }
  close() {
    if (this.closing) return;
    this.closing = true;
    for (const controller of this.active) controller.abort();
    if (this.active.size === 0) this.store.close();
  }
}
export function readingView(reading, offset = 0) {
  need(
    Number.isInteger(offset) &&
      offset >= 0 &&
      offset <= reading.segments.length,
    "INVALID_OFFSET",
  );
  const chosen = [];
  let length = 0;
  for (const segment of reading.segments.slice(offset)) {
    if (length + segment.text.length > 16000 && chosen.length) break;
    chosen.push(segment);
    length += segment.text.length;
    if (length >= 16000) break;
  }
  return {
    ...reading,
    segments: chosen,
    segment_count: reading.segments.length,
    next_offset:
      offset + chosen.length < reading.segments.length
        ? offset + chosen.length
        : null,
    source_text_partial: offset > 0 || chosen.length < reading.segments.length,
  };
}
export { safeError };
