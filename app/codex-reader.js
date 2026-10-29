/* Local-only inbox. All paper-supplied strings are inserted as text. */
(function () {
  'use strict';
  var state = null, page = 1, filter = '', selected = new Set(), loading = false;
  var labels = {candidate: '候选', queued: '待处理', running: '处理中', completed: '已完成', failed: '失败', ignored: '已忽略'};
  var sources = {manual: '手动导入', huggingface: 'Hugging Face', arxiv: 'arXiv'};
  var $ = function (id) { return document.getElementById(id); };
  function icons() { if (window.lucide) window.lucide.createIcons(); }
  function el(tag, text, className) { var node = document.createElement(tag); if (text) node.textContent = text; if (className) node.className = className; return node; }
  function notice(message, error) { $('notice').hidden = false; $('notice').textContent = message; $('notice').className = error ? 'error' : ''; }
  async function request(path, payload) {
    var options = payload === undefined ? {} : {method: 'POST', headers: {'Content-Type': 'application/json', 'X-DPR-Local': state && state.csrf || ''}, body: JSON.stringify(payload)};
    var response = await fetch('/api/codex/' + path, options), data = await response.json();
    if (!response.ok || !data.ok) throw new Error(data.error || '请求失败');
    return data;
  }
  function selection() {
    $('selected-count').textContent = selected.size + ' 篇';
    ['enqueue', 'retry', 'ignore'].forEach(function (id) { $(id).disabled = !selected.size; });
    $('select-all').checked = !!state.papers.length && state.papers.every(function (p) { return selected.has(p.id); });
  }
  function render() {
    $('worker-status').textContent = (state.codex_available ? 'Codex CLI 可用' : '未找到 Codex CLI') + ' · ' + (state.config.paused ? '队列已暂停' : state.counts.running ? '正在处理 ' + state.counts.running + ' 篇' : '队列就绪');
    $('pause').querySelector('span').textContent = state.config.paused ? '继续队列' : '暂停队列';
    $('fetch').disabled = state.fetching;
    $('fetch').querySelector('span').textContent = state.fetching ? '抓取中' : '抓取论文';
    $('filters').replaceChildren();
    [['', '全部'], ['candidate', '候选'], ['queued', '待处理'], ['running', '处理中'], ['completed', '已完成'], ['failed', '失败']].forEach(function (item) {
      var count = item[0] ? state.counts[item[0]] || 0 : Object.values(state.counts).reduce(function (a, b) { return a + b; }, 0);
      var button = el('button', item[1] + ' ' + count); button.type = 'button'; button.setAttribute('role', 'tab'); button.setAttribute('aria-selected', String(filter === item[0]));
      button.onclick = function () { filter = item[0]; page = 1; selected.clear(); refresh(); }; $('filters').append(button);
    });
    var last = state.last_fetch;
    $('fetch-status').textContent = last ? '最近抓取：' + new Date(last.finished_at * 1000).toLocaleString() + ' · 新增 ' + (last.added || 0) + ' 篇 · 自动排队 ' + (last.queued || 0) + ' 篇' + ((last.errors || []).length ? ' · ' + last.errors.map(function (e) { return e.source + ': ' + e.error; }).join('；') : '') : '尚无抓取记录';
    $('paper-list').replaceChildren();
    state.papers.forEach(function (paper) {
      var row = el('article', '', 'paper'), check = el('input'); check.type = 'checkbox'; check.checked = selected.has(paper.id); check.setAttribute('aria-label', '选择 ' + paper.title);
      check.onchange = function () { if (check.checked) selected.add(paper.id); else selected.delete(paper.id); selection(); };
      var content = el('div'), title = el('div', '', 'paper-title'), link = el('a', paper.title);
      link.href = paper.route ? '../?codex=1#/' + paper.route : paper.url;
      if (!paper.route) { link.target = '_blank'; link.rel = 'noopener noreferrer'; }
      title.append(link); content.append(title);
      var meta = el('div', '', 'paper-meta');
      meta.append(el('span', labels[paper.status] || paper.status, 'status ' + paper.status), el('span', sources[paper.origin] || paper.origin), el('span', paper.depth === 'deep' ? '精读' : '速读'), el('span', new Date(paper.created * 1000).toLocaleDateString()));
      if (paper.analysis_scope === 'partial') meta.append(el('span', '部分资料'));
      if (paper.matched_keywords.length) meta.append(el('span', paper.matched_keywords.join(' · ')));
      content.append(meta);
      if (paper.abstract) content.append(el('p', paper.abstract, 'paper-abstract'));
      if (paper.error) content.append(el('div', paper.error + (paper.status === 'queued' ? ' · 等待自动重试' : ''), 'paper-error'));
      var actions = el('div', '', 'paper-actions');
      if (paper.route) { var read = el('a', '阅读'); read.href = '../?codex=1#/' + paper.route; actions.append(read); }
      if (paper.status === 'completed' && paper.depth === 'quick') { var deepen = el('button', '精读'); deepen.onclick = function () { mutate('action', {ids: [paper.id], action: 'deepen'}); }; actions.append(deepen); }
      if (paper.attempts) { var log = el('button', '日志'); log.onclick = async function () { try { var data = await request('log/' + paper.id); $('log-content').textContent = data.log || '等待 Codex 输出'; $('log-dialog').showModal(); } catch (e) { notice(e.message, true); } }; actions.append(log); }
      row.append(check, content, actions); $('paper-list').append(row);
    });
    if (!state.papers.length) $('paper-list').append(el('div', '暂无论文', 'empty'));
    $('page-info').textContent = '共 ' + state.total + ' 篇 · 第 ' + page + ' / ' + Math.max(1, Math.ceil(state.total / 50)) + ' 页';
    $('previous').disabled = page <= 1; $('next').disabled = page * 50 >= state.total;
    selection(); icons();
  }
  async function refresh() {
    if (loading) return;
    loading = true;
    try { state = await request('state?' + new URLSearchParams({page: page, status: filter, search: $('search').value})); render(); }
    catch (error) { notice('本地服务不可用：' + error.message, true); }
    finally { loading = false; }
  }
  async function mutate(path, payload) { try { var result = await request(path, payload); await refresh(); return result; } catch (e) { notice(e.message, true); return null; } }
  $('import-form').onsubmit = async function (event) {
    event.preventDefault(); $('import-button').disabled = true;
    var result = await mutate('import', {text: $('links').value, depth: document.querySelector('input[name=depth]:checked').value});
    $('import-button').disabled = false;
    if (result) { notice('新增 ' + result.added.length + ' 篇，已有 ' + result.duplicates.length + ' 篇' + (result.errors.length ? '，导入失败 ' + result.errors.length + ' 篇：' + result.errors.map(function (e) { return e.error; }).join('；') : ''), !!result.errors.length); if (!result.errors.length) $('links').value = ''; }
  };
  $('fetch').onclick = function () { mutate('fetch', {}); };
  $('pause').onclick = function () { if (state) mutate('config', {paused: !state.config.paused}); };
  $('select-all').onchange = function () { state.papers.forEach(function (p) { if ($('select-all').checked) selected.add(p.id); else selected.delete(p.id); }); render(); };
  [['enqueue', 'enqueue'], ['retry', 'retry'], ['ignore', 'ignore']].forEach(function (entry) { $(entry[0]).onclick = async function () { var result = await mutate('action', {ids: Array.from(selected), action: entry[1], depth: $('batch-depth').value}); if (result) { selected.clear(); selection(); notice('已更新 ' + result.changed + ' 篇'); } }; });
  $('previous').onclick = function () { page--; selected.clear(); refresh(); };
  $('next').onclick = function () { page++; selected.clear(); refresh(); };
  var searchTimer;
  $('search').oninput = function () { clearTimeout(searchTimer); searchTimer = setTimeout(function () { page = 1; selected.clear(); refresh(); }, 250); };
  $('settings-open').onclick = function () {
    if (!state) return;
    var config = state.config; $('keywords').value = config.keywords.join('\n');
    document.querySelectorAll('input[name=source]').forEach(function (input) { input.checked = config.sources.includes(input.value); });
    [['days', 'days'], ['fetch-limit', 'fetch_limit'], ['auto-process', 'auto_process'], ['daily-hour', 'daily_hour']].forEach(function (entry) { $(entry[0]).value = config[entry[1]]; });
    $('daily-enabled').checked = config.daily_enabled; $('settings').showModal();
  };
  $('settings-form').onsubmit = async function (event) {
    event.preventDefault(); var result = await mutate('config', {keywords: $('keywords').value.split('\n').map(function (s) { return s.trim(); }).filter(Boolean), sources: Array.from(document.querySelectorAll('input[name=source]:checked')).map(function (s) { return s.value; }), days: Number($('days').value), fetch_limit: Number($('fetch-limit').value), auto_process: Number($('auto-process').value), daily_hour: Number($('daily-hour').value), daily_enabled: $('daily-enabled').checked});
    if (result) { $('settings').close(); notice('订阅已保存'); }
  };
  document.querySelectorAll('[data-close]').forEach(function (button) { button.onclick = function () { $(button.dataset.close).close(); }; });
  icons(); refresh(); setInterval(function () { if (!document.hidden && !document.querySelector('dialog[open]')) refresh(); }, 5000);
})();
