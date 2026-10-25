(() => {
  "use strict";
  const category=document.getElementById("resourceCategory"),version=document.getElementById("resourceVersion"),search=document.getElementById("resourceSearch"),status=document.getElementById("resourceStatus"),grid=document.getElementById("catalog");
  const states={resolved:"已列出来源文件",link_only:"在来源页面查看",source_access_required:"分享来源需要访问码／目录信息",pending_scan:"媒体地址待检查",no_download_source:"暂未解析出文件地址"};
  let data={works:[],media:{}};
  const tagsFor=work=>{
    const tags=[...(work.tags||[]),...(work.media_ids||[]).flatMap(id=>data.media[id]?.tags||[])];
    return [...new Map(tags.map(tag=>[`${tag.kind}:${tag.value}`,tag])).values()];
  };
  const versionsFor=work=>[...new Set(tagsFor(work).filter(tag=>["version","associated_version"].includes(tag.kind)).map(tag=>tag.value))];
  const compareVersions=(a,b)=>{const x=a.split(".").map(Number),y=b.split(".").map(Number);return y[0]-x[0]||y[1]-x[1];};
  function filter(works,{category="",version="",query=""}={}){
    const needles=query.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean);
    return works.filter(work=>{
      if(category&&!(work.categories||[]).includes(category))return false;
      if(version&&!versionsFor(work).includes(version))return false;
      const tags=tagsFor(work),text=[work.title,...(work.categories||[]),...tags.flatMap(t=>[t.label,t.value,...(t.aliases||[])]),
        ...(work.media_ids||[]).flatMap(id=>(data.media[id]?.origins||[]).flatMap(o=>[o.section,o.caption]))].join(" ").toLocaleLowerCase();
      return needles.every(needle=>{
        const numeric=needle.match(/^v?(\d{1,2}\.\d{1,2})$/);
        return numeric?versionsFor(work).includes(numeric[1]):text.includes(needle)||text.replace(/\s+/g,"").includes(needle);
      });
    });
  }
  window.ResourceFilters={filter,compareVersions,tagsFor,versionsFor};
  function link(label,url){const a=document.createElement("a");a.textContent=label;a.href=url;a.target="_blank";a.rel="noopener noreferrer";return a;}
  function render(){
    const works=filter(data.works,{category:category.value,version:version.value,query:search.value});grid.replaceChildren();
    status.textContent=`找到 ${works.length} / ${data.works.length} 个资源条目。`;
    if(data.status){status.textContent+=` 已检查 ${data.status.checked_entries||0} / ${data.status.index_entries||data.works.length} 份详情，${data.status.resolved_entries||0} 个条目含直接来源文件；其余分批检查。`;}
    if((data.status?.failures||[]).length)status.textContent+=" 部分来源检查未完成，已保留最近可用记录。";
    for(const work of works){
      const card=document.createElement("article"),heading=document.createElement("h2"),state=document.createElement("p"),tags=document.createElement("div"),sources=document.createElement("div");
      heading.textContent=work.title;state.className="state";state.textContent=`${(work.categories||[]).join(" / ")} · ${states[work.resolution_status]||"待核对"}`;
      tags.className="tags";tags.setAttribute("aria-label","资源标签");
      for(const tag of tagsFor(work).slice(0,12)){const chip=document.createElement("span");chip.className="tag"+(tag.confidence==="inferred"?" inferred":"");chip.textContent=tag.label;tags.append(chip);}
      sources.className="sources";sources.append(link("Wiki 出处",work.wiki_url));
      if(work.official_match)sources.append(link("官网同作品",work.official_match));
      for(const url of work.links||[])if(url!==work.official_match&&url!==work.wiki_url)sources.append(link("关联原帖／平台",url));
      card.append(heading,state,tags,sources);
      if((work.media_ids||[]).length){
        const details=document.createElement("details"),summary=document.createElement("summary"),list=document.createElement("ul");summary.textContent=`查看 ${work.media_ids.length} 个来源文件`;
        for(const [i,id] of work.media_ids.entries()){
          const item=data.media[id];if(!item)continue;const row=document.createElement("li");
          row.append(link(`${item.media_type==="video"?"视频":"图片"} ${i+1}`,item.url));
          const note=document.createElement("span");note.className="muted";note.textContent=" · 来源文件，原始质量未核验";row.append(note);list.append(row);
        }
        details.append(summary,list);card.append(details);
      }
      grid.append(card);
    }
    if(!works.length){const empty=document.createElement("p");empty.className="empty";empty.textContent="没有匹配的资源，请尝试其他关键词。";grid.append(empty);}
  }
  function option(select,value,label=value){const node=document.createElement("option");node.value=value;node.textContent=label;select.append(node);}
  [category,version].forEach(node=>node.addEventListener("change",render));search.addEventListener("input",render);
  document.getElementById("resourceClear").addEventListener("click",()=>{category.value="";version.value="";search.value="";render();});
  document.getElementById("resourceFilters").addEventListener("submit",event=>event.preventDefault());
  fetch("resources.json").then(response=>{if(!response.ok)throw new Error("unavailable");return response.json();}).then(value=>{
    data=value;const categories=[...new Set(data.works.flatMap(work=>work.categories||[]))].sort();categories.forEach(c=>option(category,c));
    const versions=[...new Set(data.works.flatMap(versionsFor))].sort(compareVersions);versions.forEach(v=>{const tag=data.works.flatMap(tagsFor).find(t=>["version","associated_version"].includes(t.kind)&&t.value===v);option(version,v,tag?.label||`版本 ${v}`);});render();
  }).catch(()=>{status.textContent="资源目录暂不可用，已有图片仍可在图片预览中查看。";});
})();
