"use strict";
// Publishes the built installer as a GitHub release, so installed apps can update themselves.
// Run `npm run release` after committing, tagging (vX.Y.Z) and pushing. Needs the GitHub CLI (gh), signed in.
// Uploads: the installer, its .blockmap (for smaller downloads) and latest.yml (what the app's
// updater reads). The notes are this version's section of RELEASE_NOTES.md.
const { execFileSync } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

const root = path.join(__dirname, "..");
const version = require(path.join(root, "package.json")).version;
const dist = path.join(root, "dist");
const exe = `OpenAYDesktop-Setup-${version}.exe`;
const files = [exe, exe + ".blockmap", "latest.yml"].map((f) => path.join(dist, f));
for (const f of files) if (!fs.existsSync(f)) throw new Error("Missing " + f + ". Run npm run dist first.");
const latest = fs.readFileSync(path.join(dist, "latest.yml"), "utf8");
if (!latest.includes(`version: ${version}`) || !latest.includes(exe)) throw new Error("dist/latest.yml doesn't match version " + version);

const notes = fs.readFileSync(path.join(root, "RELEASE_NOTES.md"), "utf8").split(/\r?\n---\r?\n/)[0];
if (!notes.includes(`# Open AY Desktop ${version}`)) throw new Error("RELEASE_NOTES.md must start with the notes for " + version);
const notesFile = path.join(os.tmpdir(), `openay-notes-${version}.md`);
fs.writeFileSync(notesFile, notes.replace(/^# .*\r?\n/, "").trim() + "\n");

execFileSync("gh", ["release", "create", `v${version}`, ...files, "--repo", "arothm/OpenAYDesktop",
                    "--title", `Open AY Desktop ${version}`, "--notes-file", notesFile, "--verify-tag"], { stdio: "inherit" });
console.log(`Published v${version}. Installed apps will find it within 6 hours, or at once with Settings > Updates > Check now.`);
