// Intentionally failing real node:test fixture producer; no external packages.
const test = require('node:test');
const assert = require('node:assert/strict');

for (let order = 0; order < 12; order++) {
  test(`checkout order ${order}`, () => {
    for (let attempt = 0; attempt < 25; attempt++) {
      console.log(JSON.stringify({ event: 'cache_probe', order, attempt, ok: true }));
    }
    if (order === 4) {
      assert.equal(41, 42, 'invalid invoice checksum for café/注文/4');
    }
    if (order === 9) {
      throw new TypeError('checkout-db.local returned an invalid invoice payload');
    }
  });
}
