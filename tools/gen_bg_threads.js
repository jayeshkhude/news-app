// Generates a subtle "thread/flow" SVG background.
// Usage:
//   node tools/gen_bg_threads.js > frontend/assets/bg-threads.svg
//
// Deterministic output (seeded RNG) so it stays stable across runs.

function mulberry32(seed) {
  let a = seed >>> 0;
  return function rand() {
    a |= 0;
    a = (a + 0x6D2B79F5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

const rand = mulberry32(1337);

const W = 1600;
const H = 900;

function field(x, y) {
  const nx = (x / W) * Math.PI * 2;
  const ny = (y / H) * Math.PI * 2;

  // Smooth-ish vector field via trig blending.
  const a = Math.sin(nx * 1.2 + ny * 0.8) + 0.55 * Math.cos(nx * 2.0 - ny * 1.7);
  const b = 0.35 * Math.sin(nx * 2.8 + ny * 2.3);
  const angle = (a + b) * Math.PI;
  return [Math.cos(angle), Math.sin(angle)];
}

const lines = Math.max(1, Math.min(80, parseInt(process.argv[2] || "26", 10) || 26));
const steps = Math.max(1, Math.min(200, parseInt(process.argv[3] || "55", 10) || 55));
const precision = Math.max(0, Math.min(2, parseInt(process.argv[4] || "1", 10) || 1));
const stepLen = 14;
const margin = 120;

const paths = [];
for (let i = 0; i < lines; i++) {
  let x = rand() * W;
  let y = rand() * H;
  let d = "M" + x.toFixed(precision) + " " + y.toFixed(precision);
  for (let s = 0; s < steps; s++) {
    const v = field(x, y);
    x += v[0] * stepLen;
    y += v[1] * stepLen;
    if (x < -margin || x > W + margin || y < -margin || y > H + margin) break;
    d += " L" + x.toFixed(precision) + " " + y.toFixed(precision);
  }
  paths.push('<path d="' + d + '"/>');
}

const svg =
  '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ' +
  W +
  " " +
  H +
  '" preserveAspectRatio="xMidYMid slice">' +
  '<g fill="none" stroke="#aa9a84" stroke-opacity="0.32" stroke-width="1.15" stroke-linecap="round" stroke-linejoin="round">' +
  paths.join("") +
  "</g></svg>";

process.stdout.write(svg);
