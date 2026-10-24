/* Static gallery: filters compose, while detail links retain original metadata indices. */
(() => {
  "use strict";
  const PAGE_SIZE = 60;
  const versionsOf = item => {
    const explicit = (item.versions || []).filter(value => value !== "未分类");
    const supported = (item.tags || []).filter(tag => tag.kind === "associated_version" && tag.confidence === "rule_supported").map(tag => tag.value);
    const values = [...new Set([...explicit, ...supported])];
    return values.length ? values : ["未分类"];
  };
  function compareVersions(a, b) {
    if (a === "未分类") return b === a ? 0 : 1;
    if (b === "未分类") return -1;
    const first = a.split(".").map(Number), second = b.split(".").map(Number);
    return second[0] - first[0] || second[1] - first[1];
  }
  const tagsOf = item => Array.isArray(item.tags) ? item.tags.map(tag => typeof tag === "string" ? {label: tag, value: tag} : tag) :
    versionsOf(item).filter(value => value !== "未分类").map(value => ({kind: "version", value, label: `版本 ${value}`, confidence: "explicit"}));
  function filterItems(items, {version = "", category = "", query = ""} = {}) {
    const needles = query.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean);
    return items.filter(item => {
      if (version && !versionsOf(item).includes(version)) return false;
      if (category && !(item.categories || ["未分类"]).includes(category)) return false;
      const tags = tagsOf(item);
      const text = [item.name, item.folder,
        ...tags.flatMap(tag => [tag.label, tag.value, ...(tag.aliases || [])]),
        ...(item.associations || []).flatMap(alias => [alias.name, alias.folder, alias.path, alias.original_path]),
        ...(item.posts || []).map(post => post.title)].join(" ").toLocaleLowerCase();
      return needles.every(needle => {
        const numericVersion = needle.match(/^v?(\d{1,2}\.\d{1,2})$/);
        if (numericVersion) return tags.some(tag => ["version", "associated_version"].includes(tag.kind) && tag.value === numericVersion[1]);
        return text.includes(needle) || text.replace(/\s+/g, "").includes(needle);
      });
    });
  }
  window.GalleryFilters = {compareVersions, filterItems, versionsOf, tagsOf};
  const grid = document.getElementById("grid");
  const version = document.getElementById("versionFilter");
  const category = document.getElementById("categoryFilter");
  const search = document.getElementById("searchFilter");
  const status = document.getElementById("filterStatus");
  let allItems = [], items = [], cursor = 0;
  function setSpan(card, image) {
    const styles = getComputedStyle(grid);
    const row = parseFloat(styles.gridAutoRows) || 4;
    const gap = parseFloat(styles.rowGap) || 12;
    const ratio = image.naturalWidth ? image.naturalHeight / image.naturalWidth : 1;
    const height = card.getBoundingClientRect().width * ratio + card.querySelector(".caption").offsetHeight;
    card.style.gridRowEnd = `span ${Math.ceil((height + gap) / (row + gap))}`;
  }
  function renderMore() {
    const end = Math.min(cursor + PAGE_SIZE, items.length);
    const fragment = document.createDocumentFragment();
    for (let i = cursor; i < end; i++) {
      const item = items[i], card = document.createElement("article"), link = document.createElement("a");
      card.className = "card";
      const params = new URLSearchParams({i: item._index});
      if (item.identity) params.set("id", item.identity);
      if (version.value) params.set("version", version.value);
      if (category.value) params.set("category", category.value);
      if (search.value) params.set("q", search.value);
      link.href = `detail.html?${params}`;
      const image = document.createElement("img");
      image.loading = "lazy"; image.src = item.thumb; image.alt = item.name || "图片预览";
      const caption = document.createElement("div"), badge = document.createElement("span"), name = document.createElement("span");
      caption.className = "caption"; badge.className = "category-badge";
      badge.textContent = (item.categories || ["未分类"]).join(" / ");
      name.className = "card-name"; name.textContent = item.name || "未命名图片"; name.title = item.name || "";
      const chips = document.createElement("div"); chips.className = "tags"; chips.setAttribute("role", "list"); chips.setAttribute("aria-label", "图片标签");
      const tags = tagsOf(item);
      for (const tag of tags.slice(0, 6)) {
        const chip = document.createElement("span"); chip.className = "tag" + (tag.confidence === "inferred" ? " tag-inferred" : "");
        chip.textContent = tag.label; chip.setAttribute("role", "listitem");
        chip.title = tag.confidence === "inferred" ? "按角色登场版本关联，非图片首次发布版本；依据可在详情中查看。" :
          tag.confidence === "rule_supported" ? "官方角色展示/PV标题与图片目录规则支持的首登场内容关联；依据可在详情中查看。" : "来源文字明确标注；依据可在详情中查看。";
        chips.append(chip);
      }
      if (tags.length > 6) { const more = document.createElement("span"); more.className = "tag-more"; more.textContent = `另有 ${tags.length - 6} 个标签，点击查看`; chips.append(more); }
      caption.append(badge, name, chips); link.append(image, caption); card.append(link); fragment.append(card);
      image.addEventListener("load", () => setSpan(card, image));
      image.addEventListener("error", () => setSpan(card, image));
    }
    grid.append(fragment); cursor = end;
    requestAnimationFrame(() => grid.querySelectorAll(".card").forEach(card => setSpan(card, card.querySelector("img"))));
  }
  function applyFilters() {
    items = filterItems(allItems, {version: version.value, category: category.value, query: search.value});
    cursor = 0; grid.replaceChildren();
    document.querySelector(".count").textContent = `${items.length} / ${allItems.length}`;
    status.textContent = `找到 ${items.length} 张图片（共 ${allItems.length} 张）`;
    if (!items.length) {
      const empty = document.createElement("p"); empty.className = "empty";
      empty.textContent = "没有符合条件的图片，试试其他标签或清除筛选。"; grid.append(empty);
    } else renderMore();
    const params = new URLSearchParams();
    if (version.value) params.set("version", version.value);
    if (category.value) params.set("category", category.value);
    if (search.value) params.set("q", search.value);
    history.replaceState(null, "", location.pathname + (params.size ? `?${params}` : ""));
  }
  function options(select, values) {
    const names = new Map(allItems.flatMap(tagsOf).filter(tag => tag.version_name).map(tag => [tag.value, tag.version_name]));
    for (const value of values) {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = select === version && names.has(value) ? `版本 ${value} · ${names.get(value)}` : value;
      select.append(option);
    }
  }
  document.getElementById("filters").addEventListener("submit", event => event.preventDefault());
  version.addEventListener("change", applyFilters); category.addEventListener("change", applyFilters);
  search.addEventListener("input", applyFilters);
  document.getElementById("clearFilters").addEventListener("click", () => { version.value = ""; category.value = ""; search.value = ""; applyFilters(); });
  const observer = new IntersectionObserver(entries => {
    if (entries.some(entry => entry.isIntersecting)) renderMore();
  }, {rootMargin: "200px"});
  observer.observe(document.getElementById("sentinel"));
  addEventListener("resize", () => grid.querySelectorAll(".card").forEach(card => setSpan(card, card.querySelector("img"))));
  async function load() {
    try {
      const response = await fetch(`meta.json?_=${Date.now()}`);
      if (!response.ok) throw new Error("Metadata request failed");
      const data = await response.json();
      allItems = (data.items || []).map((item, index) => ({...item, _index: index}));
      options(version, [...new Set(allItems.flatMap(versionsOf))].sort(compareVersions));
      const categoryOrder = ["角色图片", "月历", "活动壁纸", "测试期素材", "表情包", "影像档案", "未分类"];
      options(category, [...new Set(allItems.flatMap(item => item.categories || ["未分类"]))].sort((a, b) => {
        const rank = value => categoryOrder.includes(value) ? categoryOrder.indexOf(value) : categoryOrder.length;
        return rank(a) - rank(b) || a.localeCompare(b);
      }));
      const params = new URLSearchParams(location.search);
      version.value = params.get("version") || ""; category.value = params.get("category") || "";
      search.value = params.get("q") || "";
      if (version.value) document.getElementById("advancedFilters").open = true;
      applyFilters();
    } catch (error) { grid.replaceChildren(); status.textContent = "图片列表加载失败，请稍后重试。"; console.error(error); }
  }
  load();
})();
