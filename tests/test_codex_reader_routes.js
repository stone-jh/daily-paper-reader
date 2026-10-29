'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('app/docsify-plugin.js', 'utf8');
function load(name) {
  const start = source.indexOf('const ' + name + ' =');
  const end = source.indexOf('\n      };', start) + '\n      };'.length;
  const context = {normalizeHref: value => value};
  vm.createContext(context);
  vm.runInContext(source.slice(start, end) + ';this.fn=' + name, context);
  return context.fn;
}
const file = load('isPaperRouteFile'), href = load('isPaperHref');
assert.equal(file('codex/papers/11c35af1db691396e1353664.md'), true);
assert.equal(href('#/codex/papers/11c35af1db691396e1353664'), true);
assert.equal(file('codex/README.md'), false);
assert.equal(href('#/codex/README'), false);
assert.equal(file('201706/12/1706.03762v1-attention-is-all-you-need.md'), true);
assert.equal(file('conference/iclr-2026/paper.md'), true);
assert.equal(file('tutorial/README.md'), false);
const parse = load('parseFrontMatter');
const fields = {title: 'A title: with quotes "example"', tags: ['query:agents'], figures_json: JSON.stringify([{url: 'codex/assets/id/page-1.webp', page: 1}])};
const markdown = '---\n' + Object.entries(fields).map(([key, value]) => key + ': ' + JSON.stringify(value)).join('\n') + '\n---\nBody';
const meta = parse(markdown).meta;
assert.deepEqual(JSON.parse(JSON.stringify(meta.tags)), fields.tags);
assert.deepEqual(JSON.parse(meta.figures_json), JSON.parse(fields.figures_json));
console.log('Codex and upstream reading routes passed');
