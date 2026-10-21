let token='',offset=0;
const $=s=>document.querySelector(s);
function el(tag,text,cls){const x=document.createElement(tag);if(text!==undefined)x.textContent=text;if(cls)x.className=cls;return x}
async function api(path,options={}){const r=await fetch(path,{...options,headers:{'X-Lostmedia-Token':token,'Content-Type':'application/json',...options.headers}});if(!r.ok)throw Error((await r.text()).slice(0,300));return r.json()}
const LABELS={unseen:'à voir',maybe:'à revoir',not_bss:'pas BSS',bss:'BSS !'};
function time(s){if(s==null||s<0)return '?';return `${Math.floor(s/60)}:${String(Math.round(s%60)).padStart(2,'0')}`}
function query(){return `tier=${$('#tier').value}&verdict=${$('#verdict').value}&sort=${$('#sort').value}`}
function card(r){
  const c=el('article',undefined,'card');
  const link=el('a');link.href=r.url;link.target='_blank';link.rel='noopener noreferrer';
  const img=el('img');img.src=`/thumb/${r.id}`;img.alt=r.title;img.loading='lazy';link.append(img);
  const title=el('a',r.title,'title');title.href=r.url;title.target='_blank';title.rel='noopener noreferrer';
  const meta=el('p',`${time(r.duration)} · ${(r.published||'').slice(0,10)} · score ${r.score.toFixed(3)}${r.group_size>1?` · groupe de ${r.group_size}`:''}`,'muted');
  const buttons=el('div',undefined,'verdicts');
  for(const v of ['not_bss','maybe','bss']){
    const b=el('button',LABELS[v]);if(r.verdict===v)b.className='active';
    b.onclick=async()=>{try{await api('/api/review',{method:'POST',body:JSON.stringify({video_id:r.id,verdict:v})});r.verdict=v;for(const x of buttons.children)x.className='';b.className='active'}catch(e){$('#message').textContent=e.message}};
    buttons.append(b);
  }
  c.append(link,title,meta,buttons);
  if(r.group_size>1)c.classList.add('grouped');
  return c;
}
async function load(reset){
  if(reset){offset=0;$('#grid').replaceChildren()}
  try{
    const d=await api(`/api/candidates?${query()}&limit=120&offset=${offset}`);
    $('#summary').textContent=`${d.total} vidéo(s) dans ce filtre · déjà notées : ${d.reviewed.map(x=>`${LABELS[x.verdict]||x.verdict} ${x.n}`).join(', ')||'aucune'}`;
    $('#grid').append(...d.rows.map(card));
    offset+=d.rows.length;
    $('#more').hidden=offset>=d.total;
    $('#csv').href=`/api/candidates.csv?tier=${$('#tier').value}&verdict=all`;
  }catch(e){$('#message').textContent=e.message}
}
(async()=>{token=(await api('/api/session')).token;for(const id of ['#tier','#verdict','#sort'])$(id).onchange=()=>load(true);$('#more').onclick=()=>load(false);load(true)})();
