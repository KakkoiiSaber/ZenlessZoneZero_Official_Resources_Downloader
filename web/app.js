'use strict';
const $ = selector => document.querySelector(selector);
const state = {data: null, category: '', query: '', entry: null, groups: new Map()};
const symbols = {'73': '▷', '72': '▶', '98': '▧', '99': '♡'};
let gallery = [], position = 0, previousFocus, viewerPoster;
const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
let closingViewer = false;
let listObserver;
let listLayout;
let viewerMode = 'auto';
let viewerRequest = 0;

async function readyImage(url) {
  const image = new Image();
  image.src = url;
  await image.decode();
  return image;
}
function warmNeighbours(resources, index) {
  for (const neighbour of [resources[index - 1], resources[index + 1]]) {
    if (neighbour?.available && neighbour.kind === 'image') readyImage(neighbour.url).catch(() => {});
  }
}

function masonryLayout(grid) {
  let frame = 0;
  const layout = () => {
    frame = 0;
    if (!grid.isConnected) return;
    const style = getComputedStyle(grid);
    const columns = Number(style.getPropertyValue('--columns')) || 3;
    const gap = Number.parseFloat(style.getPropertyValue('--gap')) || 18;
    const width = (grid.clientWidth - gap * (columns - 1)) / columns;
    const heights = Array(columns).fill(0);
    for (const card of grid.children) card.style.width = `${width}px`;
    for (const card of grid.children) {
      const column = heights.indexOf(Math.min(...heights));
      card.style.left = `${column * (width + gap)}px`;
      card.style.top = `${heights[column]}px`;
      heights[column] += card.getBoundingClientRect().height + gap;
    }
    grid.style.height = `${Math.max(0, ...heights) - (grid.children.length ? gap : 0)}px`;
  };
  const resize = new ResizeObserver(() => {
    if (!frame) frame = requestAnimationFrame(layout);
  });
  resize.observe(grid);
  return {
    append(cards) { cards.forEach(card => resize.observe(card)); layout(); },
    destroy() { resize.disconnect(); cancelAnimationFrame(frame); }
  };
}

function transition(node, frames, duration = 180) {
  if (reducedMotion.matches || !node.animate) return null;
  node.getAnimations().forEach(animation => animation.cancel());
  return node.animate(frames, {duration, easing: 'cubic-bezier(.2,.7,.2,1)'});
}
function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}
function size(bytes) {
  if (!bytes) return '';
  let n = bytes;
  for (const unit of ['B', 'KB', 'MB', 'GB']) {
    if (n < 1000 || unit === 'GB') return `${n.toFixed(unit === 'B' ? 0 : 1)} ${unit}`;
    n /= 1000;
  }
}
function uniqueResources(entries) {
  return new Map(entries.flatMap(e => e.groups.flatMap(g => g.resources.map(r => [r.id, r]))));
}
function setRoute(category = '', entry = null) {
  const hash = new URLSearchParams();
  if (category) hash.set('category', category);
  if (entry) hash.set('entry', entry);
  location.hash = hash.toString();
  if (state.category === category && state.entry === entry) render();
}
function readRoute() {
  const hash = new URLSearchParams(location.hash.slice(1));
  state.category = hash.get('category') || '';
  state.entry = hash.get('entry');
  $('#sidebar').classList.remove('open');
  $('#menu').setAttribute('aria-expanded', 'false');
  render();
  window.scrollTo({top: 0, behavior: 'instant'});
}
function filteredGroups(entry) {
  const titleMatch = entry.title.toLocaleLowerCase().includes(state.query);
  return entry.groups.map((group, index) => ({...group, index,
    resources: group.resources.filter(r => !state.query || titleMatch || `${group.name} ${r.caption || ''} ${r.filename}`.toLocaleLowerCase().includes(state.query))}))
    .filter(group => group.resources.length);
}
function renderNavigation() {
  const nav = $('#categories');
  if (nav.children.length) {
    for (const link of nav.children) {
      const active = link.dataset.category === state.category;
      link.classList.toggle('active', active);
      if (active) link.setAttribute('aria-current', 'page');
      else link.removeAttribute('aria-current');
    }
    return;
  }
  for (const [id, name] of [['', '全部资源'], ...Object.entries(state.data.categories)]) {
    const entries = state.data.entries.filter(e => !id || e.categories.includes(id));
    const link = element('a', `nav-link${state.category === id ? ' active' : ''}`);
    link.dataset.category = id;
    if (state.category === id) link.setAttribute('aria-current', 'page');
    const params = new URLSearchParams(id ? {category: id} : {});
    link.href = `#${params}`;
    link.append(element('span', 'nav-symbol', symbols[id] || '◈'), element('span', '', name), element('small', '', String(entries.length)));
    nav.append(link);
  }
}
function empty(message) {
  const section = element('section', 'empty-state');
  section.append(element('div', 'empty-icon', '▧'), element('h2', '', message), element('p', '', '可切换分类、修改搜索词，或刷新本地目录。'));
  return section;
}
function render() {
  renderContent();
  transition($('#content'), [{opacity: .35, transform: 'translateY(7px)'}, {opacity: 1, transform: 'translateY(0)'}]);
}
function renderContent() {
  listObserver?.disconnect();
  listLayout?.destroy();
  if (!state.data) return;
  renderNavigation();
  const all = state.data.entries.filter(e => !state.category || e.categories.includes(state.category));
  const title = state.data.categories[state.category] || '全部资源';
  const detail = state.entry && all.find(e => e.id === state.entry);
  document.title = `${detail ? detail.title : title} · 绝区零资源档案`;
  const content = $('#content');
  content.replaceChildren();
  $('#status').textContent = '';
  if (detail) {
    const back = element('button', 'back-button', '‹ 返回主题列表');
    back.onclick = () => setRoute(state.category);
    content.append(back);
    renderDetail(detail, content);
    return;
  }
  const matching = all.filter(e => filteredGroups(e).length);
  if (!matching.length) { content.append(empty('没有符合条件的资源')); return; }
  const grid = element('div', 'entry-grid');
  const sentinel = element('div', 'load-sentinel');
  sentinel.setAttribute('aria-hidden', 'true');
  content.append(grid, sentinel);
  listLayout = masonryLayout(grid);
  let loaded = 0;
  const appendBatch = () => {
    if (!grid.isConnected) return;
    const start = loaded;
    loaded = Math.min(loaded + 24, matching.length);
    const fragment = document.createDocumentFragment();
    for (const entry of matching.slice(start, loaded)) {
      const button = element('button', 'entry-card');
      const groups = filteredGroups(entry);
      const items = [...uniqueResources([{groups}]).values()];
      const image = items.find(r => r.available && r.kind === 'image');
      const preview = element('div', 'entry-preview');
      if (entry.cover || image) {
        const img = element('img'); img.src = entry.cover || image.url; img.loading = 'lazy'; img.alt = '';
        if (entry.preview_size) [img.width, img.height] = entry.preview_size;
        preview.append(img);
        if (items.some(r => r.kind === 'video')) preview.append(element('span', 'preview-play', '▷'));
      } else {
        preview.classList.add(items[0]?.kind === 'video' ? 'video-placeholder' : 'image-placeholder');
        preview.append(element('span', 'preview-glyph', items[0]?.kind === 'video' ? '▷' : '▧'));
      }
      const text = element('div', 'entry-text');
      text.append(element('h2', '', entry.title), element('p', '', `${items.length} 个资源 · ${items.filter(r => r.available).length} 已下载`));
      button.append(preview, text);
      button.onclick = () => setRoute(state.category, entry.id);
      fragment.append(button);
    }
    const added = [...fragment.children];
    grid.append(fragment);
    listLayout.append(added);
    if (start) added.forEach(card => transition(card, [{opacity: .4}, {opacity: 1}]));
    if (loaded < matching.length) observer.observe(sentinel);
    else { observer.disconnect(); sentinel.remove(); }
  };
  const observer = new IntersectionObserver(entries => {
    if (!entries.some(entry => entry.isIntersecting)) return;
    observer.unobserve(sentinel);
    appendBatch();
  }, {rootMargin: '500px 0px'});
  listObserver = observer;
  appendBatch();
}
function renderDetail(entry, content) {
  const groups = filteredGroups(entry);
  if (!groups.length) {content.append(empty('当前主题没有符合条件的资源')); return;}
  if (groups.length > 1) {
    const tabs = element('nav', 'section-tabs'); tabs.setAttribute('aria-label', '主题分组');
    for (const group of groups) {
      const a = element('button', '', group.name);
      a.onclick = () => document.getElementById(`group-${group.index}`).scrollIntoView({behavior: reducedMotion.matches ? 'instant' : 'smooth', block: 'start'});
      tabs.append(a);
    }
    content.append(tabs);
  }
  for (const group of groups) content.append(renderGroup(entry, group));
}
function renderGroup(entry, group, preparedImage) {
  const section = element('section', 'group-panel'); section.id = `group-${group.index}`;
  const heading = element('div', 'group-heading');
  heading.append(element('h2', '', group.name), element('small', '', `${group.resources.length} 个资源`));
  section.append(heading);
  const selectionKey = `${entry.id}:${group.index}`;
  const selected = state.groups.get(selectionKey) || 0;
  const index = Math.min(selected, group.resources.length - 1);
  const pills = element('div', 'pills');
  let request = 0;
  group.resources.forEach((resource, i) => {
    const pill = element('button', i === index ? 'selected' : '', resource.caption || String(Number.parseInt(resource.filename, 10) || i + 1));
    pill.title = (resource.caption || resource.filename) + (resource.available ? '' : ' · 尚未下载');
    pill.setAttribute('aria-pressed', String(i === index));
    pill.onclick = async () => {
      const currentRequest = ++request;
      section.removeAttribute('aria-busy');
      if (i === index) return;
      let image;
      if (resource.available && resource.kind === 'image') {
        section.setAttribute('aria-busy', 'true');
        try { image = await readyImage(resource.url); }
        catch {
          if (currentRequest === request && section.isConnected) {
            section.removeAttribute('aria-busy');
            pill.title = '图片加载失败，点击重试';
          }
          return;
        }
      }
      if (currentRequest !== request || !section.isConnected) return;
      state.groups.set(selectionKey, i);
      const replacement = renderGroup(entry, group, image);
      section.replaceWith(replacement);
      replacement.querySelectorAll('.pills button')[i]?.focus({preventScroll: true});
    };
    pills.append(pill);
  });
  if (group.resources.length > 1) section.append(pills);
  const resource = group.resources[index];
  const stage = element('div', 'resource-stage');
  if (resource.available) {
    if (resource.kind === 'image') {
      const trigger = element('button', 'image-trigger');
      trigger.setAttribute('aria-label', `放大 ${group.name} ${resource.caption || resource.filename}`);
      const img = preparedImage || element('img');
      if (!preparedImage) img.src = resource.url;
      img.alt = `${group.name} ${resource.caption || resource.filename}`; img.loading = 'lazy';
      if (img.complete && img.naturalWidth) warmNeighbours(group.resources, index);
      else img.addEventListener('load', () => {
        if (section.isConnected) warmNeighbours(group.resources, index);
      }, {once: true});
      trigger.append(img);
      trigger.onclick = () => openViewer(entry, group, index);
      stage.append(trigger);
    } else {
      const video = element('video'); video.src = resource.url; video.controls = true; video.preload = 'metadata'; video.playsInline = true;
      if (entry.cover) video.poster = entry.cover;
      stage.append(video);
    }
  } else {
    const placeholder = element('div', 'missing');
    placeholder.append(element('span', 'preview-glyph', resource.kind === 'video' ? '▷' : '▧'), element('strong', '', '尚未下载到本地'), element('p', '', '下载完成后刷新页面即可查看。'));
    stage.append(placeholder);
  }
  section.append(stage);
  const caption = element('div', 'resource-caption');
  caption.append(element('span', '', `${resource.caption || (resource.kind === 'video' ? entry.title : resource.filename)}${resource.size ? ` · ${size(resource.size)}` : ''}`));
  if (resource.available) {
    const save = element('a', '', '保存原文件 ↓'); save.href = resource.url; save.download = resource.filename; caption.append(save);
  } else caption.append(element('span', 'muted', '未下载'));
  section.append(caption);
  return section;
}
function openViewer(entry, group, index) {
  gallery = group.resources.filter(r => r.available);
  position = gallery.findIndex(r => r.id === group.resources[index].id);
  previousFocus = document.activeElement;
  viewerPoster = entry.cover;
  viewerMode = 'auto';
  $('#viewer-title').textContent = entry.title;
  $('#viewer-caption').textContent = group.name;
  drawViewer(); $('#viewer').showModal();
  transition($('#viewer'), [{opacity: 0, transform: 'translateY(10px) scale(.98)'}, {opacity: 1, transform: 'translateY(0) scale(1)'}], 200);
}
async function drawViewer() {
  const request = ++viewerRequest;
  const item = gallery[position];
  let media;
  if (item.kind === 'image') {
    $('#player').setAttribute('aria-busy', 'true');
    try { media = await readyImage(item.url); }
    catch {
      if (request === viewerRequest) {
        $('#player').removeAttribute('aria-busy');
        $('#counter').textContent = '图片加载失败，请切换后重试';
      }
      return;
    }
  } else { media = element('video'); media.src = item.url; }
  if (request !== viewerRequest) return;
  $('#player').removeAttribute('aria-busy');
  if (item.kind === 'video') {
    media.controls = true;
    if (viewerPoster) media.poster = viewerPoster;
  } else media.alt = item.caption || item.filename;
  $('#player').replaceChildren(media);
  applyViewerMode();
  warmNeighbours(gallery, position);
  $('#counter').textContent = `${position + 1} / ${gallery.length} · ${item.caption || item.filename}${item.size ? ` · ${size(item.size)}` : ''}`;
  $('#save').href = item.url; $('#save').download = item.filename;
  $('#previous').disabled = position === 0; $('#next').disabled = position === gallery.length - 1;
}
function applyViewerMode() {
  const image = $('#player img');
  const wide = Boolean(image && (viewerMode === 'width' || (viewerMode === 'auto' && image.naturalHeight > image.naturalWidth * 1.3)));
  $('#viewer').classList.toggle('read-wide', wide);
  $('#view-mode').hidden = !image;
  $('#view-mode').textContent = wide ? '整图适屏' : '适宽阅读';
  $('#view-mode').setAttribute('aria-pressed', String(wide));
  $('.viewer-stage').scrollTo({top: 0, left: 0, behavior: 'instant'});
}
$('#view-mode').onclick = () => {
  viewerMode = $('#viewer').classList.contains('read-wide') ? 'fit' : 'width';
  applyViewerMode();
};
async function closeViewer() {
  if (closingViewer || !$('#viewer').open) return;
  closingViewer = true;
  const animation = transition($('#viewer'), [{opacity: 1, transform: 'scale(1)'}, {opacity: 0, transform: 'scale(.985)'}], 130);
  if (animation) await animation.finished.catch(() => {});
  $('#viewer').close();
  closingViewer = false;
}
$('#close').onclick = closeViewer;
$('#viewer').addEventListener('cancel', event => {event.preventDefault(); closeViewer();});
$('#viewer').addEventListener('close', () => { viewerRequest++; $('#player').replaceChildren(); $('#player').removeAttribute('aria-busy'); previousFocus?.focus(); });
$('#previous').onclick = () => {if (position > 0) {position--; drawViewer();}};
$('#next').onclick = () => {if (position < gallery.length - 1) {position++; drawViewer();}};
$('#viewer').addEventListener('keydown', event => {
  if (event.target.tagName === 'VIDEO') return;
  if (event.key === 'ArrowLeft') $('#previous').click();
  if (event.key === 'ArrowRight') $('#next').click();
});
$('#viewer').addEventListener('click', event => {if (event.target === $('#viewer')) closeViewer();});
$('#search').oninput = event => {state.query = event.target.value.trim().toLocaleLowerCase(); render();};
$('#menu').onclick = () => { const open = $('#sidebar').classList.toggle('open'); $('#menu').setAttribute('aria-expanded', String(open)); };
async function loadCatalog() {
  $('#status').textContent = '正在读取本地目录…';
  try {
    const response = await fetch('/api/catalog', {cache: 'no-store'});
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    state.data = await response.json(); readRoute();
  } catch (error) {
    $('#status').textContent = `无法读取本地目录（${error.message}）。请检查 Wiki 服务与 metafiles/wiki.json 后重试。`;
  }
}
window.addEventListener('hashchange', readRoute);
loadCatalog();
