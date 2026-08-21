const { authorized, sendUnauthorized } = require("./_serve");
const { updateSupabaseState } = require("./_receipts_data");

async function readBody(req) {
  const chunks = [];
  for await (const chunk of req) chunks.push(chunk);
  const raw = Buffer.concat(chunks).toString("utf8");
  return raw ? JSON.parse(raw) : {};
}

module.exports = async (req, res) => {
  if (!authorized(req)) {
    sendUnauthorized(res);
    return;
  }
  if (req.method !== "PATCH" && req.method !== "POST") {
    res.statusCode = 405;
    res.end("Method not allowed");
    return;
  }
  try {
    const body = await readBody(req);
    if (!body.id || !body.patch || typeof body.patch !== "object") {
      res.statusCode = 400;
      res.end("Missing id or patch");
      return;
    }
    const result = await updateSupabaseState(body.id, body.patch);
    res.setHeader("Content-Type", "application/json; charset=utf-8");
    res.end(JSON.stringify({ ok: true, result }));
  } catch (error) {
    res.statusCode = error.status || 500;
    res.setHeader("Content-Type", "application/json; charset=utf-8");
    res.end(JSON.stringify({ ok: false, error: error.message }));
  }
};
