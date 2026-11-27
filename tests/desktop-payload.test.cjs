const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const { createHash } = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');

const moduleUnderTest = import('../desktop/scripts/python-archive.mjs');

test('all supported desktop builds select a pinned archive, and a custom build needs its own digest', async () => {
  const { pythonArchive } = await moduleUnderTest;
  const targets = ['aarch64-apple-darwin', 'x86_64-apple-darwin', 'x86_64-pc-windows-msvc'];
  const pins = targets.map(target => pythonArchive(target, {}));
  assert.equal(new Set(pins.map(p => p.sha256)).size, targets.length);
  for (const [index, pin] of pins.entries()) {
    assert.match(pin.url, /github\.com\/astral-sh\/python-build-standalone\/releases\/download\/\d+\//);
    assert.ok(pin.url.includes(targets[index]));
    assert.match(pin.sha256, /^[0-9a-f]{64}$/);
  }
  assert.throws(() => pythonArchive('unknown', {}), /Unsupported/);
  const target = targets[0], url = 'https://mirror.example/python.tar.gz';
  for (const env of [{ PBS_URL: url }, { PBS_SHA256: pins[0].sha256 }, { PBS_URL: url, PBS_SHA256: 'invalid' }]) {
    assert.throws(() => pythonArchive(target, env), /PBS_SHA256/);
  }
  assert.deepEqual(pythonArchive(target, { PBS_URL: url, PBS_SHA256: pins[0].sha256.toUpperCase() }), {
    url, sha256: pins[0].sha256,
  });
});

test('only the matching archive reaches tar, including across redirects', async t => {
  const { unpackPythonArchive } = await moduleUnderTest;
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'textfold-payload-'));
  t.after(() => fs.rmSync(temp, { recursive: true, force: true }));
  const destination = path.join(temp, 'payload');
  fs.mkdirSync(destination);
  fs.writeFileSync(path.join(destination, 'previous'), 'existing build');
  fs.writeFileSync(path.join(temp, 'fixture'), 'new build');
  const bytes = execFileSync('tar', ['-czf', '-', 'fixture'], { cwd: temp });
  const spec = { url: 'https://release.example/runtime', sha256: createHash('sha256').update(bytes).digest('hex') };

  // A valid tar file with different contents would extract successfully without the digest check.
  fs.writeFileSync(path.join(temp, 'fixture'), 'other build');
  const different = execFileSync('tar', ['-czf', '-', 'fixture'], { cwd: temp });
  await assert.rejects(unpackPythonArchive(spec, destination, async () => new Response(different)), /SHA-256 mismatch/);
  assert.equal(fs.readFileSync(path.join(destination, 'previous'), 'utf8'), 'existing build');
  assert.equal(fs.existsSync(path.join(destination, 'fixture')), false);

  await assert.rejects(unpackPythonArchive(spec, destination, async () => Response.redirect('http://mirror.example/')), /HTTPS/);
  const visits = [];
  await unpackPythonArchive(spec, destination, async (url, options) => {
    assert.equal(options.redirect, 'manual');
    visits.push(url.href);
    return visits.length === 1 ? Response.redirect('https://mirror.example/archive') : new Response(bytes);
  });
  assert.deepEqual(visits, [spec.url, 'https://mirror.example/archive']);
  assert.deepEqual(fs.readdirSync(destination), ['fixture']);
  assert.equal(fs.readFileSync(path.join(destination, 'fixture'), 'utf8'), 'new build');
});
