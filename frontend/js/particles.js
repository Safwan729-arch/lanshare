/**
 * The drifting constellation behind the app.
 *
 * Decoration, deliberately cheap. It sits on a fixed canvas behind every
 * panel, never takes a pointer event, and stops entirely when the page is
 * hidden or the user has asked for reduced motion.
 *
 * It reads `--accent` from the stylesheet rather than carrying its own colour,
 * so it follows the light and dark themes instead of fighting them.
 */

/** Roughly one particle per this many CSS pixels of canvas. */
const AREA_PER_PARTICLE = 22000;

/** Sparse enough to read as a constellation rather than a dot. */
export const MIN_PARTICLES = 8;

/**
 * The ceiling matters more than it looks: every frame compares each particle
 * with every other one, so the link pass is O(n^2). Sixty particles is 1,770
 * distance checks per frame, which a phone can do while also uploading a file.
 */
export const MAX_PARTICLES = 60;

/** How close two particles must be before a line joins them. */
const LINK_DISTANCE = 110;

/** Kept well under 1 so body text keeps its contrast. */
const DOT_ALPHA = 0.55;
const LINK_ALPHA = 0.35;

/** Fallback if `--accent` cannot be read - matches the dark theme's accent. */
const FALLBACK_ACCENT = '#4b86ff';

/**
 * How many particles a canvas this size should hold.
 *
 * Scaled by area, not fixed: a count that looks right on a phone is lost on a
 * desktop, and one that looks right on a desktop is a soup on a phone.
 * Returns 0 for a canvas with no size - before layout, or while hidden -
 * because seeding into it would stack every particle at the origin.
 */
export function particleCountFor(width, height) {
  const area = width * height;
  if (!Number.isFinite(area) || area <= 0) return 0;
  return Math.min(MAX_PARTICLES, Math.max(MIN_PARTICLES, Math.round(area / AREA_PER_PARTICLE)));
}

/** Whether to be running frames at all. */
export function shouldAnimate({ reducedMotion, pageHidden }) {
  return !reducedMotion && !pageHidden;
}

/**
 * The theme's accent at a given alpha.
 *
 * `--accent` is authored as hex, but `getComputedStyle` can hand back
 * something else entirely - `rgb()`, a named colour, or an empty string while
 * a theme change is in flight. Anything unparseable falls back rather than
 * building `rgba(NaN, ...)`, which draws nothing and looks like a broken
 * animation rather than a bad colour.
 */
export function withAlpha(color, alpha) {
  const hex = String(color).trim();
  const short = /^#([0-9a-f])([0-9a-f])([0-9a-f])$/i.exec(hex);
  const long = /^#([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(hex);

  let parts;
  if (long) {
    parts = [long[1], long[2], long[3]].map((pair) => parseInt(pair, 16));
  } else if (short) {
    parts = [short[1], short[2], short[3]].map((digit) => parseInt(digit + digit, 16));
  } else {
    return withAlpha(FALLBACK_ACCENT, alpha);
  }

  return `rgba(${parts[0]}, ${parts[1]}, ${parts[2]}, ${alpha})`;
}

/**
 * Attach the animation to a canvas. Returns a function that stops it.
 *
 * Everything is kept inside this closure - no module-level mutable state - so
 * the module can be imported without doing anything until it is asked to.
 */
export function start(canvas) {
  const ctx = canvas.getContext && canvas.getContext('2d');
  if (!ctx) return () => {};

  const motion = window.matchMedia('(prefers-reduced-motion: reduce)');
  let particles = [];
  let frame = null;
  let width = 0;
  let height = 0;
  let accent = readAccent();

  function readAccent() {
    const value = getComputedStyle(document.documentElement).getPropertyValue('--accent');
    return value.trim() || FALLBACK_ACCENT;
  }

  function seed() {
    const count = particleCountFor(width, height);
    particles = Array.from({ length: count }, () => ({
      x: Math.random() * width,
      y: Math.random() * height,
      vx: (Math.random() - 0.5) * 0.35,
      vy: (Math.random() - 0.5) * 0.35,
      radius: Math.random() * 1.6 + 0.9,
    }));
  }

  function resize() {
    // Scale the backing store by the device pixel ratio, or the whole thing is
    // blurry on exactly the phones this app is built for.
    const ratio = window.devicePixelRatio || 1;
    width = canvas.clientWidth;
    height = canvas.clientHeight;
    canvas.width = Math.round(width * ratio);
    canvas.height = Math.round(height * ratio);
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    seed();
    draw();
  }

  function step() {
    for (const p of particles) {
      p.x += p.vx;
      p.y += p.vy;
      if (p.x < 0 || p.x > width) p.vx *= -1;
      if (p.y < 0 || p.y > height) p.vy *= -1;
    }
  }

  function draw() {
    ctx.clearRect(0, 0, width, height);
    ctx.fillStyle = withAlpha(accent, DOT_ALPHA);

    for (let i = 0; i < particles.length; i += 1) {
      const p = particles[i];
      ctx.beginPath();
      ctx.arc(p.x, p.y, p.radius, 0, Math.PI * 2);
      ctx.fill();

      // Start at i + 1 so each pair is considered once, not twice.
      for (let j = i + 1; j < particles.length; j += 1) {
        const other = particles[j];
        const dx = p.x - other.x;
        const dy = p.y - other.y;
        const distance = Math.hypot(dx, dy);
        if (distance >= LINK_DISTANCE) continue;

        ctx.beginPath();
        ctx.moveTo(p.x, p.y);
        ctx.lineTo(other.x, other.y);
        ctx.strokeStyle = withAlpha(accent, (1 - distance / LINK_DISTANCE) * LINK_ALPHA);
        ctx.stroke();
      }
    }
  }

  function loop() {
    step();
    draw();
    frame = requestAnimationFrame(loop);
  }

  /** Start or stop the loop to match what the page and the user want. */
  function sync() {
    const wanted = shouldAnimate({ reducedMotion: motion.matches, pageHidden: document.hidden });
    if (wanted && frame === null) {
      frame = requestAnimationFrame(loop);
    } else if (!wanted && frame !== null) {
      cancelAnimationFrame(frame);
      frame = null;
      draw(); // leave a still frame rather than a blank rectangle
    }
  }

  function onThemeChange() {
    accent = readAccent();
    if (frame === null) draw();
  }

  const observer = new ResizeObserver(resize);
  observer.observe(canvas);
  const scheme = window.matchMedia('(prefers-color-scheme: dark)');

  document.addEventListener('visibilitychange', sync);
  motion.addEventListener('change', sync);
  scheme.addEventListener('change', onThemeChange);

  resize();
  sync();

  return () => {
    if (frame !== null) cancelAnimationFrame(frame);
    frame = null;
    observer.disconnect();
    document.removeEventListener('visibilitychange', sync);
    motion.removeEventListener('change', sync);
    scheme.removeEventListener('change', onThemeChange);
  };
}
