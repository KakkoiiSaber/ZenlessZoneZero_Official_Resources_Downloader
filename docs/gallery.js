/* Static gallery: filters compose, while detail links retain original metadata indices. */
(() => {
  "use strict";
  const PAGE_SIZE = 60;
  const versionsOf = item => item.versions?.length ? item.versions : ["未分类"];
  function compareVersions(a, b) {
    if (a === "未分类") return b === a ? 0 : 1;
    if (b === "未分类") return -1;
    const first = a.split(".").map(Number), second = b.split(".").map(Number);
    return second[0] - first[0] || second[1] - first[1];
  }
  function filterItems(items, {version = "", category = "", query = ""} = {}) {
    const needle = query.trim().toLocaleLowerCase();
    return items.filter(item => (!version || versionsOf(item).includes(version)) &&
      (!category || (item.categories || ["其他"]).includes(category)) &&
      (!needle || [item.name, item.folder,
        ...(item.associations || []).flatMap(alias => [alias.name, alias.folder, alias.path]),
        ...(item.posts || []).map(post => post.title)].join(" ").toLocaleLowerCase().includes(needle)));
  }
  window.GalleryFilters = {compareVersions, filterItems, versionsOf};
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
      caption.className = "caption"; badge.className = "version-badge";
      badge.textContent = versionsOf(item).map(value => value === "未分类" ? value : `版本 ${value}`).join(" / ");
      name.className = "card-name"; name.textContent = item.name || "未命名图片"; name.title = item.name || "";
      caption.append(badge, name); link.append(image, caption); card.append(link); fragment.append(card);
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
      empty.textContent = "没有符合条件的图片，试试其他版本或清除筛选。"; grid.append(empty);
    } else renderMore();
    const params = new URLSearchParams();
    if (version.value) params.set("version", version.value);
    if (category.value) params.set("category", category.value);
    if (search.value) params.set("q", search.value);
    history.replaceState(null, "", location.pathname + (params.size ? `?${params}` : ""));
  }
  function options(select, values) {
    for (const value of values) { const option = document.createElement("option"); option.value = value; option.textContent = value; select.append(option); }
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
      options(category, [...new Set(allItems.flatMap(item => item.categories || ["其他"]))].sort());
      const params = new URLSearchParams(location.search);
      version.value = params.get("version") || ""; category.value = params.get("category") || "";
      search.value = params.get("q") || "";
      applyFilters();
    } catch (error) { grid.replaceChildren(); status.textContent = "图片列表加载失败，请稍后重试。"; console.error(error); }
  }
  load();
})();
