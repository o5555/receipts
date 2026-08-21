const fs = require("fs");
const path = require("path");

const DATA_DIR = path.join(process.cwd(), "protected", "data");
const RECEIPTS_FILE = path.join(DATA_DIR, "receipts.json");
const STATE_FILE = path.join(DATA_DIR, "state.json");

const VALID_TAGS = new Set(["business", "personal", "needs-tag", "ignore"]);
const VALID_STATUSES = new Set([
  "ready",
  "needs-receipt",
  "pending-payout",
  "done",
  "needs-review",
  "blocked",
  "ignore",
]);

function readJson(file, fallback) {
  try {
    return JSON.parse(fs.readFileSync(file, "utf8"));
  } catch {
    return fallback;
  }
}

function localPayload(reason = "local-file") {
  const receipts = readJson(RECEIPTS_FILE, { items: [] });
  const state = readJson(STATE_FILE, { state: {} });
  return {
    source: reason,
    items: Array.isArray(receipts.items) ? receipts.items : [],
    state: state.state && typeof state.state === "object" ? state.state : {},
    stateMeta: state,
  };
}

function supabaseConfig() {
  const url = process.env.SUPABASE_URL;
  const key = process.env.SUPABASE_SERVICE_ROLE_KEY;
  if (!url || !key) return null;
  return { url: url.replace(/\/$/, ""), key };
}

async function supabaseRequest(pathname, options = {}) {
  const config = supabaseConfig();
  if (!config) throw new Error("Missing Supabase config");
  const response = await fetch(`${config.url}/rest/v1/${pathname}`, {
    ...options,
    headers: {
      apikey: config.key,
      authorization: `Bearer ${config.key}`,
      "content-type": "application/json",
      ...(options.headers || {}),
    },
  });
  const text = await response.text();
  const data = text ? JSON.parse(text) : null;
  if (!response.ok) {
    const error = new Error(data?.message || `Supabase returned ${response.status}`);
    error.status = response.status;
    error.data = data;
    throw error;
  }
  return data;
}

async function readSupabasePayload() {
  if (!supabaseConfig()) return localPayload("local-file-missing-supabase-env");
  try {
    const [receiptRows, stateRows] = await Promise.all([
      supabaseRequest("receipts?select=id,data&order=id.asc"),
      supabaseRequest("receipt_state?select=receipt_id,tag,status,operator_note,updated_at,updated_by"),
    ]);
    const state = {};
    for (const row of stateRows || []) {
      state[row.receipt_id] = {
        ...(row.tag ? { tag: row.tag } : {}),
        ...(row.status ? { status: row.status } : {}),
        ...(row.operator_note ? { operatorNote: row.operator_note } : {}),
        ...(row.updated_at ? { updatedAt: row.updated_at } : {}),
        ...(row.updated_by ? { updatedBy: row.updated_by } : {}),
      };
    }
    return {
      source: "supabase",
      items: (receiptRows || []).map(row => row.data).filter(Boolean),
      state,
      stateMeta: {
        schemaVersion: 1,
        source: "supabase",
        updatedAt: new Date().toISOString(),
      },
    };
  } catch (error) {
    return localPayload(`local-file-supabase-${error.status || "error"}`);
  }
}

function sanitizePatch(patch) {
  const clean = {};
  if (patch.tag !== undefined) {
    if (!VALID_TAGS.has(patch.tag)) throw new Error(`Invalid tag: ${patch.tag}`);
    clean.tag = patch.tag;
  }
  if (patch.status !== undefined) {
    if (!VALID_STATUSES.has(patch.status)) throw new Error(`Invalid status: ${patch.status}`);
    clean.status = patch.status;
  }
  if (patch.operatorNote !== undefined) clean.operator_note = String(patch.operatorNote);
  clean.updated_at = new Date().toISOString();
  clean.updated_by = patch.updatedBy || "dashboard";
  return clean;
}

async function updateSupabaseState(receiptId, patch) {
  if (!supabaseConfig()) throw new Error("Missing Supabase config");
  const body = {
    receipt_id: receiptId,
    ...sanitizePatch(patch),
  };
  return supabaseRequest("receipt_state?on_conflict=receipt_id", {
    method: "POST",
    headers: {
      Prefer: "resolution=merge-duplicates,return=representation",
    },
    body: JSON.stringify(body),
  });
}

module.exports = {
  localPayload,
  readSupabasePayload,
  sanitizePatch,
  updateSupabaseState,
};
