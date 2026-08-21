const { serve } = require("./_serve");

module.exports = (req, res) => {
  const parts = req.query.path || [];
  const requestPath = Array.isArray(parts) ? parts.join("/") : String(parts);
  serve(req, res, requestPath || "index.html");
};
