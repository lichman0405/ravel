"use strict";

const path = require("node:path");

const appRoot = path.resolve(__dirname, "..", "..", "..", "..", "apps", "desktop");

function requireDist(name) {
  return require(path.join(appRoot, "dist", "lib", `${name}.cjs`));
}

function readSource(...parts) {
  const fs = require("node:fs");
  return fs.readFileSync(path.join(appRoot, ...parts), "utf8");
}

module.exports = { appRoot, requireDist, readSource };
