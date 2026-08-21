const { serve } = require("./_serve");

module.exports = (req, res) => serve(req, res, "index.html");
