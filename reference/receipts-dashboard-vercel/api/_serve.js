const fs = require("fs");
const path = require("path");

const ROOT = path.join(process.cwd(), "protected");

const MIME = {
  ".html": "text/html; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".js": "application/javascript; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".csv": "text/csv; charset=utf-8",
  ".pdf": "application/pdf",
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".svg": "image/svg+xml",
};

function timingSafeEqual(a, b) {
  const aa = Buffer.from(a || "");
  const bb = Buffer.from(b || "");
  if (aa.length !== bb.length) return false;
  return require("crypto").timingSafeEqual(aa, bb);
}

function authorized(req) {
  const user = process.env.DASHBOARD_USER || "oscar";
  const pass = process.env.DASHBOARD_PASSWORD;
  if (!pass) return false;

  const header = req.headers.authorization || "";
  if (!header.startsWith("Basic ")) return false;

  const decoded = Buffer.from(header.slice(6), "base64").toString("utf8");
  const split = decoded.indexOf(":");
  if (split === -1) return false;

  return (
    timingSafeEqual(decoded.slice(0, split), user) &&
    timingSafeEqual(decoded.slice(split + 1), pass)
  );
}

function safeTarget(requestPath) {
  const clean = (requestPath || "index.html").replace(/^\/+/, "");
  const target = path.normalize(path.join(ROOT, clean));
  if (!target.startsWith(ROOT)) return null;
  return target;
}

function sendUnauthorized(res) {
  res.statusCode = 401;
  res.setHeader("WWW-Authenticate", 'Basic realm="Receipts Dashboard"');
  res.end("Authentication required");
}

function serve(req, res, requestPath) {
  if (!authorized(req)) {
    sendUnauthorized(res);
    return;
  }

  const target = safeTarget(requestPath);
  if (!target) {
    res.statusCode = 400;
    res.end("Bad request");
    return;
  }

  let stat;
  try {
    stat = fs.statSync(target);
  } catch {
    res.statusCode = 404;
    res.end("Not found");
    return;
  }

  const file = stat.isDirectory() ? path.join(target, "index.html") : target;
  const ext = path.extname(file).toLowerCase();
  res.setHeader("Content-Type", MIME[ext] || "application/octet-stream");
  fs.createReadStream(file).pipe(res);
}

module.exports = { authorized, sendUnauthorized, serve };
