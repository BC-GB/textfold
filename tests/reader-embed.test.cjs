const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const reader = fs.readFileSync(path.join(__dirname, '..', 'textfold', 'web', 'reader.js'), 'utf8');
const embeds = reader.slice(reader.indexOf('  const framable ='), reader.indexOf('  function toggleUnit('));

test('the external figure stays a link until the reader chooses to load it', () => {
  const elements = [];
  function h(tag) {
    const el = {
      tag, style: {},
      append() {}, prepend() {}, replaceWith() {},
      addEventListener(event, fn) { this[event] = fn; },
      setAttribute(key, value) { this[key] = value; },
    };
    elements.push(el);
    return el;
  }
  const figure = { href: 'https://figures.example.org/chart', title: 'Chart', className: 'pf-h-300', replaceWith() {} };
  const context = {
    URL, h, location: new URL('https://reader.example.org'), document: { createElement: h, createTextNode: String },
    T: { en: { embed_load: 'Load interactive figure', embed_open: 'Open it on its own', embed_live: 'Interactive' } },
    ui: 'en', body: { querySelectorAll: () => [figure] },
  };
  vm.runInNewContext(embeds + '\nembeds(body);', context);
  const load = elements.find(el => el.tag === 'button');
  assert.match(load.textContent, /figures\.example\.org/);
  assert.equal(elements.find(el => el.tag === 'a').href, figure.href);
  assert.equal(elements.filter(el => el.tag === 'iframe').length, 0);
  load.click();
  const frame = elements.find(el => el.tag === 'iframe');
  assert.equal(frame.src, figure.href);
  assert.equal(frame.title, 'Chart');
  assert.equal(frame.referrerPolicy, 'no-referrer');
  assert.match(frame.sandbox, /allow-scripts/);
});
