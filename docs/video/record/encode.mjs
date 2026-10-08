// Turns each recorded take (record/out/<take>/frames + frames.json) into a
// constant-30-fps H.264 clip at ../project/assets/<take>.mp4, keeping real timing:
// a frame lasts until the next one arrived (the screencast only sends a frame
// when the page changes). 2880x1620 leaves room for punch-ins up to ~1.5x
// at 1920x1080 without softening. Also copies markers to ../project/assets/<take>.markers.json.
//
//   node docs/video/record/encode.mjs [take ...]
import fs from "node:fs";
import path from "node:path";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const OUT = path.join(HERE, "out");
const ASSETS = path.join(HERE, "..", "project", "assets");
const FFMPEG = process.env.FFMPEG || "/Users/afernandezde/.local/bin/ffmpeg";
fs.mkdirSync(ASSETS, { recursive: true });

const takes = process.argv.slice(2).length ? process.argv.slice(2) : fs.readdirSync(OUT).filter((d) => fs.existsSync(path.join(OUT, d, "frames.json")));
for (const take of takes) {
  const dir = path.join(OUT, take);
  const { end, frames } = JSON.parse(fs.readFileSync(path.join(dir, "frames.json"), "utf8"));
  const lines = ["ffconcat version 1.0"];
  frames.forEach((f, i) => {
    const next = i + 1 < frames.length ? frames[i + 1].t : Math.max(end, f.t + 1 / 30);
    lines.push(`file '${f.file}'`, `duration ${Math.max(0.001, next - f.t).toFixed(4)}`);
  });
  lines.push(`file '${frames.at(-1).file}'`);
  const list = path.join(dir, "frames.ffconcat");
  fs.writeFileSync(list, lines.join("\n") + "\n");
  const mp4 = path.join(ASSETS, `${take}.mp4`);
  execFileSync(FFMPEG, ["-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", list,
    "-vf", "fps=30,scale=2880:1620:flags=lanczos,format=yuv420p",
    "-c:v", "libx264", "-preset", "slow", "-crf", "16", "-g", "30", "-movflags", "+faststart", "-an", mp4], { stdio: "inherit" });
  fs.copyFileSync(path.join(dir, "markers.json"), path.join(ASSETS, `${take}.markers.json`));
  console.log(`${take}: ${end.toFixed(2)} s -> ${path.relative(process.cwd(), mp4)}`);
}
