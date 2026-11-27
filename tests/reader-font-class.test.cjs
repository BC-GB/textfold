const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const reader = fs.readFileSync(path.join(__dirname, '..', 'textfold', 'web', 'reader.js'), 'utf8');
const start = reader.indexOf('  function textHtml(uid, L) {');
const textHtml = reader.slice(start, reader.indexOf('\n  // ── math', start));
const escapeHtml = reader.match(/  const esc = \(s\) => [^\n]+;/)[0];

function render(font) {
  const context = vm.createContext({
    pieces: () => [{ tok: { t: 'Safe', f: font } }],
    A: {}, hlCls: () => '', fdCls: () => '', rvCls: () => '', rvDel: () => '',
  });
  return vm.runInContext(`${escapeHtml}\n${textHtml}\ntextHtml('u1', 4)`, context);
}

test('cold text rendering escapes font classes without changing valid classes', () => {
  assert.equal(render('ltx_font_italic'), '<span class="tr ltx_font_italic">Safe</span>');
  const html = render('ltx_font_bad" onanimationstart="fetch(/api/agents/probe)');
  assert.equal(html, '<span class="tr ltx_font_bad&quot; onanimationstart=&quot;fetch(/api/agents/probe)">Safe</span>');
  assert.doesNotMatch(html, / onanimationstart="/);
});
