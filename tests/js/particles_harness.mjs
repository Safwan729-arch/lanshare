/**
 * Tests for the decisions inside the background animation.
 *
 * The rendering itself is not tested - there is no DOM harness and inventing
 * one for decoration would not earn its keep. What is tested is the three
 * things that would actually go wrong unnoticed: too many particles on a big
 * screen, an animation that ignores `prefers-reduced-motion`, and a link
 * colour built by string-mangling the theme's accent.
 *
 * Invoked by tests/test_upload_stall.py with the module URL as argv[2].
 */
import assert from 'node:assert/strict';

const { particleCountFor, shouldAnimate, withAlpha, MIN_PARTICLES, MAX_PARTICLES } = await import(
  process.argv[2]
);

const tests = {
  'the particle count follows the area': () => {
    const phone = particleCountFor(390, 844);
    const desktop = particleCountFor(1920, 1080);
    assert.ok(
      desktop > phone,
      `a desktop got ${desktop} and a phone ${phone}; density must follow area, not be fixed`
    );
  },

  'a big screen cannot run away with it': () => {
    // The link pass is O(n^2): 60 particles is already 1770 distance checks a frame.
    const huge = particleCountFor(5120, 2880);
    assert.ok(huge <= MAX_PARTICLES, `${huge} particles on a 5K display is past the cap`);
  },

  'a small screen still shows something': () => {
    const tiny = particleCountFor(200, 120);
    assert.ok(tiny >= MIN_PARTICLES, `${tiny} particles is too sparse to read as a constellation`);
  },

  'a canvas with no size asks for nothing': () => {
    // Happens before layout, and on a hidden element. Seeding into it would
    // put every particle at 0,0.
    assert.equal(particleCountFor(0, 0), 0);
    assert.equal(particleCountFor(-10, 50), 0);
    assert.equal(particleCountFor(Number.NaN, 50), 0);
  },

  'reduced motion stops the animation': () => {
    assert.equal(
      shouldAnimate({ reducedMotion: true, pageHidden: false }),
      false,
      'the app honours prefers-reduced-motion everywhere else; this must too'
    );
  },

  'a hidden page stops the animation': () => {
    // A phone in someone's pocket should not be drawing frames.
    assert.equal(shouldAnimate({ reducedMotion: false, pageHidden: true }), false);
  },

  'an ordinary visible page animates': () => {
    assert.equal(shouldAnimate({ reducedMotion: false, pageHidden: false }), true);
  },

  'the accent colour survives being given an alpha': () => {
    assert.equal(withAlpha('#4b86ff', 0.5), 'rgba(75, 134, 255, 0.5)');
    assert.equal(withAlpha('  #2f6df6  ', 1), 'rgba(47, 109, 246, 1)');
  },

  'a short hex accent still works': () => {
    assert.equal(withAlpha('#abc', 0.25), 'rgba(170, 187, 204, 0.25)');
  },

  'an accent it cannot parse does not produce garbage': () => {
    // getComputedStyle can hand back rgb(), a named colour, or nothing at all
    // mid-theme-change. Returning "rgba(NaN, NaN, NaN, 0.5)" would silently
    // draw nothing and look like a broken animation.
    const result = withAlpha('rebeccapurple', 0.5);
    assert.ok(!/NaN/.test(result), `unparseable accent produced ${result}`);
  },
};

let passed = 0;
let failed = 0;
for (const [name, fn] of Object.entries(tests)) {
  try {
    await fn();
    console.log(`  ok   ${name}`);
    passed += 1;
  } catch (error) {
    console.log(`  FAIL ${name}\n       ${error.message}`);
    failed += 1;
  }
}
console.log(`${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
