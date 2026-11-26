"use strict";
// Downloads the official Windows "embeddable" Python from python.org, checks its SHA-256 against
// the value pinned below, and unpacks it into build/python, which the installer bundles
// (see "extraResources" in package.json). The download is cached in build/, so it happens once.
//
// To move to a newer Python: update VERSION and SHA256 from
// https://www.python.org/downloads/ (the "Windows embeddable package (64-bit)" file).
const crypto = require("crypto");
const fs = require("fs");
const path = require("path");
const { spawnSync } = require("child_process");

const VERSION = "3.15.2";
const SHA256 = "d012f3aa29cf07436dae8aea1fec0fd195c2612a0b059b1f915ae0afb9ad1f62";
const URL = `https://www.python.org/ftp/python/${VERSION}/python-${VERSION}-embed-amd64.zip`;

const root = path.join(__dirname, "..");
const buildDir = path.join(root, "build");
const zipPath = path.join(buildDir, `python-${VERSION}-embed-amd64.zip`);
const outDir = path.join(buildDir, "python");
const marker = path.join(outDir, ".openay-python-version");

const sha256 = (buf) => crypto.createHash("sha256").update(buf).digest("hex");

async function main() {
  if (fs.existsSync(marker) && fs.readFileSync(marker, "utf8").trim() === VERSION) {
    console.log(`Python ${VERSION} is already in build/python.`);
    return;
  }
  fs.mkdirSync(buildDir, { recursive: true });
  let data = fs.existsSync(zipPath) ? fs.readFileSync(zipPath) : null;
  if (!data || sha256(data) !== SHA256) {
    console.log(`Downloading ${URL}`);
    const r = await fetch(URL);
    if (!r.ok) throw new Error(`Download failed: HTTP ${r.status}`);
    data = Buffer.from(await r.arrayBuffer());
  }
  const got = sha256(data);
  if (got !== SHA256) throw new Error(`Checksum mismatch for ${path.basename(zipPath)}: expected ${SHA256}, got ${got}`);
  fs.writeFileSync(zipPath, data);

  fs.rmSync(outDir, { recursive: true, force: true });
  fs.mkdirSync(outDir, { recursive: true });
  // Windows 10+ ships bsdtar, which reads .zip. Call it by full path: Git Bash's GNU tar would read "D:" as a host name.
  const systemTar = path.join(process.env.SystemRoot || "C:\\Windows", "System32", "tar.exe");
  const tar = spawnSync(fs.existsSync(systemTar) ? systemTar : "tar", ["-xf", zipPath, "-C", outDir], { stdio: "inherit" });
  if (tar.status !== 0) throw new Error("Unpacking Python failed (tar exit code " + tar.status + ").");
  if (!fs.existsSync(path.join(outDir, "pythonw.exe"))) throw new Error("pythonw.exe is missing from the Python package.");
  fs.writeFileSync(marker, VERSION + "\n");
  console.log(`Python ${VERSION} (SHA-256 verified) unpacked to build/python.`);
}

main().catch((e) => { console.error(e.message || e); process.exit(1); });
