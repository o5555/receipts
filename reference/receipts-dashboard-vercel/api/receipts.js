const { authorized, sendUnauthorized } = require("./_serve");
const { readSupabasePayload } = require("./_receipts_data");

module.exports = async (req, res) => {
  if (!authorized(req)) {
    sendUnauthorized(res);
    return;
  }
  if (req.method !== "GET") {
    res.statusCode = 405;
    res.end("Method not allowed");
    return;
  }
  const payload = await readSupabasePayload();
  res.setHeader("Content-Type", "application/json; charset=utf-8");
  res.end(JSON.stringify(payload));
};
