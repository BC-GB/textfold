// The runtime the app ships, in desktop/payload:
//   python/   a standalone CPython (python-build-standalone, the "install_only_stripped" build for the app's system:
//             Apple silicon, an Intel Mac, 64-bit Windows) with TextFold's packages (requirements.txt, and the Anthropic SDK) installed into it
//   app/      the code: textfold/ (its web/ inside)
// On a Mac electron-builder copies it into the app's Resources and signs every Mach-O file in it with the app (hardened
// runtime, build/entitlements.mac.plist), which notarization requires. Parts of CPython the server never uses
// (tkinter and its Tcl/Tk, the test suite, IDLE) are left out: less to sign, a smaller app.
//
//   node scripts/prepare-payload.mjs                  for this computer
//   node scripts/prepare-payload.mjs darwin x64       an Intel Mac's, with the x86_64 Rust target and Xcode command line tools
//   PBS_URL=<https url> PBS_SHA256=<trusted sha256> node scripts/...     a given build
// A Windows payload is made on Windows: the packages are installed by the Python it ships.
import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { cpSync, existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { pythonArchive, pythonVersion, unpackPythonArchive } from './python-archive.mjs';
import { fileURLToPath } from 'node:url';

const desktop = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const root = path.dirname(desktop);
const payload = path.join(desktop, 'payload');
const [platform = process.platform, arch = process.arch] = process.argv.slice(2);
const TARGETS = { 'darwin-arm64': 'aarch64-apple-darwin', 'darwin-x64': 'x86_64-apple-darwin', 'win32-x64': 'x86_64-pc-windows-msvc' };
const TARGET = TARGETS[`${platform}-${arch}`];
if (!TARGET) throw new Error(`no payload for ${platform}-${arch}: ${Object.keys(TARGETS).join(', ')}`);
if ((platform === 'win32') !== (process.platform === 'win32')) throw new Error(`a ${platform} payload is made on ${platform}`);
const WIN = platform === 'win32';

const archive = pythonArchive(TARGET, process.env);
console.log('CPython:', TARGET, archive.sha256);
await unpackPythonArchive(archive, payload);
const py = WIN ? path.join(payload, 'python', 'python.exe') : path.join(payload, 'python', 'bin', 'python3');
// an Intel Mac's Python on Apple silicon runs under Rosetta, and pip then picks the Intel wheels
const run = (args, opts) => execFileSync(platform === 'darwin' && arch !== process.arch ? 'arch' : py,
  platform === 'darwin' && arch !== process.arch ? [arch === 'x64' ? '-x86_64' : '-arm64', py, ...args] : args, opts);

// Custom archives must support the same Python series and security patch floor.
run(['-I', '-S', '-c',
  'import sys; floor=tuple(map(int, sys.argv[1].split("."))); ' +
  'sys.exit(0 if sys.version_info[:2] == floor[:2] and sys.version_info[:3] >= floor else "Unexpected Python version for desktop payload")',
  pythonVersion], { stdio: 'inherit' });

let lockFile = path.join(desktop, `requirements-${WIN ? 'win' : 'mac'}-${arch}.txt`);
let wheelDir;
try {
  if (platform === 'darwin' && arch === 'x64') {
    wheelDir = mkdtempSync(path.join(os.tmpdir(), 'textfold-mac-crypto-'));
    const script = path.join(desktop, 'scripts', 'build-mac-intel-crypto.sh');
    const args = [script, py, wheelDir, path.join(desktop, 'requirements-mac-x64-crypto-build.txt')];
    if (process.arch === 'x64') {
      execFileSync('/bin/bash', args, { stdio: 'inherit' });
    } else {
      execFileSync('arch', ['-x86_64', '/bin/bash', ...args], { stdio: 'inherit' });
    }
    const wheels = readdirSync(wheelDir).filter((name) => /^cryptography-50\.0\.2-.*-macosx_.*_x86_64\.whl$/.test(name));
    if (wheels.length !== 1) throw new Error('Expected one cryptography 50.0.2 wheel for Intel macOS');
    const digest = createHash('sha256').update(readFileSync(path.join(wheelDir, wheels[0]))).digest('hex');
    const locked = readFileSync(lockFile, 'utf8');
    lockFile = path.join(wheelDir, 'requirements.txt');
    writeFileSync(lockFile, `${locked}cryptography==50.0.2 --hash=sha256:${digest}\n`);
  }
  console.log('Installing locked TextFold packages…');
  const localWheel = wheelDir ? ['--find-links', wheelDir] : [];
  run(['-I', '-m', 'pip', '--isolated', 'install', '--no-cache-dir', '--disable-pip-version-check', '--no-warn-script-location',
    '--index-url', 'https://pypi.org/simple', '--only-binary=:all:', '--require-hashes', ...localWheel, '-r', lockFile],
  { stdio: 'inherit' });
} finally {
  if (wheelDir) rmSync(wheelDir, { recursive: true, force: true });
}

// what the server never uses
const stdlib = WIN ? path.join(payload, 'python', 'Lib')
  : path.join(payload, 'python', 'lib', readdirSync(path.join(payload, 'python', 'lib')).find((d) => /^python3\.\d+$/.test(d)));
for (const p of ['test', 'idlelib', 'tkinter', 'turtledemo', 'ensurepip', 'lib2to3']) rmSync(path.join(stdlib, p), { recursive: true, force: true });
const dynload = WIN ? path.join(payload, 'python', 'DLLs') : path.join(stdlib, 'lib-dynload');
for (const f of readdirSync(dynload)) if (/^(_tkinter|tcl\d|tk\d)/.test(f)) rmSync(path.join(dynload, f));
const tcl = WIN ? path.join(payload, 'python') : path.join(payload, 'python', 'lib');
for (const d of readdirSync(tcl)) if (/^(tcl|tk|itcl|thread)\d/i.test(d) || /^lib(tcl|tk)/.test(d) || (WIN && d === 'tcl')) rmSync(path.join(tcl, d), { recursive: true, force: true });
(function prune(dir) {
  for (const e of readdirSync(dir, { withFileTypes: true })) {
    if (!e.isDirectory()) continue;
    if (e.name === '__pycache__') rmSync(path.join(dir, e.name), { recursive: true, force: true });
    else prune(path.join(dir, e.name));
  }
})(path.join(payload, 'python'));

// the code
const app = path.join(payload, 'app');
mkdirSync(app, { recursive: true });
for (const d of ['textfold']) {
  cpSync(path.join(root, d), path.join(app, d), { recursive: true, filter: (src) => !src.includes('__pycache__') });
}
writeFileSync(path.join(app, 'requirements.txt'), '# installed into ../python\n');

// a quick check that the runtime imports what the server needs (trafilatura: web pages and HTML files)
run(['-s', '-c', 'import bs4, lxml, PIL, trafilatura, mammoth, openpyxl, pdf_inspector, pypdfium2, jwt, cryptography, textfold.server; print("payload ok")'], { cwd: app, stdio: 'inherit' });
if (!existsSync(path.join(app, 'textfold', 'web', 'reader.js'))) throw new Error('web/ missing from the payload');
