const { serve } = require("./_serve");

module.exports = (req, res) => {
  const requestPath = req.query.path ? String(req.query.path) : "index.html";
  serve(req, res, requestPath);
};
