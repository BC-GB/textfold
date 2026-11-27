// To update CPython, review the release and replace all three archive hashes
// from https://github.com/astral-sh/python-build-standalone/releases/download/20261124/SHA256SUMS.
import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { mkdirSync, rmSync } from 'node:fs';

export const pythonVersion = '3.12.16';
const release = '20261124';
const sha256ByTarget = {
  'aarch64-apple-darwin': '9d6d9d0d85feda3f9cb4eb3e048f0fed21ed280feda087927eaa4fbc7124826f',
  'x86_64-apple-darwin': '6d7ef4efa1adf65f312e6e865040ce03ef8a4bd7e1b45415193295c9ffc0dbdd',
  'x86_64-pc-windows-msvc': '96694b50dcfde13d56e40346ddaa552a9cd650290e9b933cecf83c1d82226960',
};

export function pythonArchive(target, env) {
  if (!Object.hasOwn(sha256ByTarget, target)) throw new Error('Unsupported standalone CPython target');
  if (env.PBS_URL !== undefined || env.PBS_SHA256 !== undefined) {
    if (!env.PBS_URL || !/^[0-9a-f]{64}$/i.test(env.PBS_SHA256 || '')) {
      throw new Error('Set PBS_URL and a trusted PBS_SHA256 together for a custom Python build');
    }
    return { url: env.PBS_URL, sha256: env.PBS_SHA256.toLowerCase() };
  }
  const filename = `cpython-${pythonVersion}+${release}-${target}-install_only_stripped.tar.gz`;
  return {
    url: `https://github.com/astral-sh/python-build-standalone/releases/download/${release}/${encodeURIComponent(filename)}`,
    sha256: sha256ByTarget[target],
  };
}

export async function unpackPythonArchive(spec, destination, fetchArchive = fetch) {
  let url = new URL(spec.url);
  for (let redirects = 0; redirects <= 10; redirects++) {
    if (url.protocol !== 'https:' || url.username || url.password) {
      throw new Error('The Python archive needs an HTTPS URL without embedded credentials');
    }
    const response = await fetchArchive(url, { redirect: 'manual' });
    if ([301, 302, 303, 307, 308].includes(response.status)) {
      const location = response.headers.get('location');
      await response.body?.cancel();
      if (!location) throw new Error('The Python archive redirect has no destination');
      url = new URL(location, url);
      continue;
    }
    if (!response.ok) throw new Error(`Python archive download: HTTP ${response.status}`);
    const archive = Buffer.from(await response.arrayBuffer());
    if (createHash('sha256').update(archive).digest('hex') !== spec.sha256) {
      throw new Error('Python archive SHA-256 mismatch');
    }
    rmSync(destination, { recursive: true, force: true });
    mkdirSync(destination, { recursive: true });
    // Windows tar and GNU tar receive exactly the bytes that passed the hash check.
    execFileSync('tar', ['-xzf', '-'], { input: archive, cwd: destination, stdio: ['pipe', 'inherit', 'inherit'] });
    return;
  }
  throw new Error('Too many Python archive redirects');
}
