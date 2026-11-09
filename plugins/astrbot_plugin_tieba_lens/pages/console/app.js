/* AstrBot injects the official SDK before this module executes. No token storage. */
const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const escapeHTML = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const icon = name => `<svg class="icon" aria-hidden="true"><use href="#i-${name}"/></svg>`;
const STATUS = {queued:'等待处理', fetching:'读取内容', rendering:'生成卡片', sending:'发送卡片', done:'已完成', failed:'失败', delivery_failed:'发送失败'};
const ACTIVE = new Set(['queued', 'fetching', 'rendering', 'sending']);
const FAILED = new Set(['failed', 'delivery_failed']);
const resultReady = job => job?.result_ready === true || job?.status === 'done';
const VIEWS = {overview:'工作总览', parse:'手动解析', settings:'偏好设置', history:'任务历史', maintenance:'缓存与诊断'};
const THEMES = {
  paper:{name:'暖纸',detail:'纸纹 · 书简',description:'暖纸细纹与书简印记，适合长文慢读。',font:'霞鹜文楷'},
  midnight:{name:'深夜',detail:'星空 · 月光',description:'深蓝星空与一弯月光，适合低光阅读。',font:'霞鹜文楷'},
  mist:{name:'清雾',detail:'远山 · 水墨',description:'薄雾叠山与清浅水墨，留出安静的阅读空间。',font:'霞鹜文楷'},
  denia:{name:'达妮娅',detail:'Q版 · 泡影',description:'官方 Q 版达妮娅与虹彩泡影，搭配柔和的手写笔意。',font:'霞鹜文楷'},
  miku:{name:'初音未来',detail:'双马尾 · 音律',description:'葱色双马尾与音律格纹，清亮而有节奏。',font:'霞鹜文楷'},
  bilibili:{name:'哔哩哔哩',detail:'小电视 · 弹幕',description:'粉蓝小电视与弹幕边饰，轻快地收起一段讨论。',font:'霞鹜文楷'},
};
const THEME_OPTIONS = Object.fromEntries(Object.entries(THEMES).map(([key,value]) => [key,value.name]));
const FONT_OPTIONS = {lxgw:'霞鹜文楷',custom:'自定义字体'};
const RENDER_BACKENDS = {auto:'自动选择',service:'复用 AstrBot 渲染服务',local:'本地浏览器'};
const BROWSER_ENGINES = {headless:'精简 Chromium',chromium:'完整 Chromium'};
const FONT_LIMIT = 32 * 1024 * 1024;
const FONT_CHUNK_CHARACTERS = 524288;
const EMPTY_FONT = {uploaded:false,name:'',size:0,sha256:'',max_bytes:FONT_LIMIT,formats:['ttf','otf','woff','woff2']};
const themeArt = key => `<svg class="theme-art" aria-hidden="true"><use href="#theme-${key}"/></svg>`;
const GROUP_KEYS = ['enabled','auto_parse','output_mode','forward_original_images','theme','font_override','single_image','comment_limit','subcomment_limit','forum_thread_limit','sort','only_author','include_images','include_emoji','image_limit','show_source','reactions','cooldown_seconds','dedup_seconds'];
const DEFAULTS = {enabled:true,auto_parse:true,allow_private:true,output_mode:'auto',forward_original_images:false,theme:'denia',font_override:'lxgw',render_backend:'auto',browser_engine:'headless',single_image:true,comment_limit:12,subcomment_limit:6,forum_thread_limit:10,sort:'hot',only_author:false,include_images:true,include_emoji:true,image_limit:40,image_width:960,page_height:3000,max_pages:12,show_source:true,reactions:true,reaction_start:'289',reaction_done:'124',reaction_failed:'38',dedup_seconds:120,cooldown_seconds:5,cache_minutes:30,retention_hours:24,max_cache_mb:256,request_timeout:20,request_delay:1,retries:1,max_concurrent:2,max_links:3,blacklist:[],group_overrides:{},cookie_set:false};
const SECTIONS = [
  {id:'general',title:'基础行为',description:'决定插件何时接收内容、如何发送结果。',fields:[
    {key:'enabled',label:'启用贴吧拾光',type:'boolean',help:'控制机器人中的自动解析功能'},
    {key:'auto_parse',label:'自动识别分享',type:'boolean',help:'识别消息里的贴吧链接与分享卡片'},
    {key:'allow_private',label:'允许私聊解析',type:'boolean',help:'在机器人私聊中接收解析请求'},
    {key:'output_mode',label:'结果发送方式',type:'select',options:{auto:'自动选择',card:'图片卡片',forward:'合并转发'},help:'自动模式按内容和平台能力选择'},
    {key:'forward_original_images',label:'聊天记录附带正文原图',type:'boolean',help:'开启后 QQ 会将渲染卡片与正文原图放进同一条聊天记录，先卡片后原图，按帖子顺序排列'},
    {key:'max_links',label:'单条消息链接上限',type:'number',min:1,max:5,help:'同一条消息最多处理 1～5 个链接'},
  ]},
  {id:'content',title:'内容范围',description:'按阅读需要控制楼层、楼中楼、配图与吧内列表。',fields:[
    {key:'comment_limit',label:'回复楼层数',type:'number',min:0,max:100,help:'不含首楼，0 表示仅保留首楼'},
    {key:'subcomment_limit',label:'每楼的楼中楼回复数',type:'number',min:0,max:20,help:'未收录的回复会在卡片中注明'},
    {key:'forum_thread_limit',label:'吧首页帖子数',type:'number',min:1,max:30,help:'每次吧内速览收录的帖子数量'},
    {key:'sort',label:'回复排序',type:'select',options:{default:'原楼层顺序',latest:'最新回复',hot:'点赞数从高到低'},help:'热度按已获取回复的点赞数降序排列，首楼固定最前；接口未提供的点赞数不作 0 处理'},
    {key:'only_author',label:'只看楼主',type:'boolean',help:'帖子中仅保留楼主发表的内容'},
    {key:'include_images',label:'展示配图',type:'boolean',help:'按原内容顺序保留帖子图片'},
    {key:'include_emoji',label:'展示贴吧表情',type:'boolean',help:'保留源站表情与可读文字占位'},
    {key:'image_limit',label:'内容图片数量上限',type:'number',min:0,max:100,help:'受大小、耗时与安全下载预算共同约束'},
  ]},
  {id:'render',title:'卡片与分页',description:'连续排成整张长图，或按指定高度分页。',fields:[
    {key:'theme',label:'默认卡片主题',type:'select',options:THEME_OPTIONS,help:'六种主题各有自己的形象、纹理和推荐字体'},
    {key:'font_override',label:'卡片字体',type:'select',options:FONT_OPTIONS,help:'默认使用霞鹜文楷；自定义字体在下方上传后可用于全局、分群或本次解析'},
    {key:'single_image',label:'优先生成整张长图',type:'boolean',help:'一次排完全部内容；超过渲染长度时再续图'},
    {key:'image_width',label:'卡片宽度（像素）',type:'number',min:640,max:1200,help:'推荐 960，范围 640～1200'},
    {key:'page_height',label:'分页模式目标高度（像素）',type:'number',min:1600,max:5000,help:'关闭整张长图时生效，范围 1600～5000'},
    {key:'max_pages',label:'每次图片张数上限',type:'number',min:1,max:30,help:'达到上限会提示后续内容未收录'},
    {key:'show_source',label:'展示来源信息',type:'boolean',help:'显示卡片底部来源链接、读取说明及收录/未展开数量提示；保存后对新解析生效'},
  ]},
  {id:'renderer',title:'渲染管理',description:'选择卡片生成环境，下载所需浏览器并查看准备进度。',fields:[
    {key:'render_backend',label:'渲染方式',type:'select',options:RENDER_BACKENDS,help:'自动模式优先复用已配置的渲染服务和可用系统浏览器；选择后统一保存生效'},
    {key:'browser_engine',label:'本地浏览器类型',type:'select',options:BROWSER_ENGINES,help:'精简 Chromium 占用较低，推荐日常出图；完整 Chromium 适合需要完整浏览器的环境'},
  ]},
  {id:'reactions',title:'消息反馈',description:'使用 QQ 贴表情表示开始、完成或失败。',fields:[
    {key:'reactions',label:'启用状态贴表情',type:'boolean',help:'是否生效取决于当前平台和账号能力'},
    {key:'reaction_start',label:'开始处理 · 表情 ID',type:'reaction',help:'填写 QQ 支持的数字贴表情 ID'},
    {key:'reaction_done',label:'处理完成 · 表情 ID',type:'reaction',help:'全部结果发送完成后的状态反馈'},
    {key:'reaction_failed',label:'处理失败 · 表情 ID',type:'reaction',help:'任务失败时的状态反馈'},
  ]},
  {id:'requests',title:'请求与并发',description:'为源站与本地渲染保留合理的资源余量。',fields:[
    {key:'dedup_seconds',label:'相同链接去重（秒）',type:'number',min:0,max:3600,help:'相同会话中的重复分享间隔；0 表示关闭'},
    {key:'cooldown_seconds',label:'会话冷却时间（秒）',type:'number',min:0,max:300,help:'自动解析之间的最短间隔'},
    {key:'request_timeout',label:'单次请求超时（秒）',type:'number',min:5,max:60,help:'源站请求的最长等待时间'},
    {key:'request_delay',label:'源站请求间隔（秒）',type:'number',min:0.5,max:10,step:0.1,help:'共享请求节奏，最低 0.5 秒'},
    {key:'retries',label:'请求重试次数',type:'number',min:0,max:3,help:'临时失败时最多额外重试 0～3 次'},
    {key:'max_concurrent',label:'同时处理任务数',type:'number',min:1,max:4,help:'更高并发会增加内存和源站请求压力'},
  ]},
  {id:'storage',title:'缓存与留存',description:'平衡重复访问速度与本地存储占用。',fields:[
    {key:'cache_minutes',label:'内容缓存时间（分钟）',type:'number',min:0,max:1440,help:'0 表示不复用缓存；最长 24 小时'},
    {key:'retention_hours',label:'任务与产物留存（小时）',type:'number',min:1,max:168,help:'历史结果保留 1～168 小时'},
    {key:'max_cache_mb',label:'缓存容量上限（MB）',type:'number',min:32,max:2048,help:'达到预算时由后端执行缓存清理'},
  ]},
];
const FIELDS = Object.fromEntries(SECTIONS.flatMap(section => section.fields).map(field => [field.key, field]));
const S = {bridge:null,online:false,settings:{...DEFAULTS},settingsLoaded:false,dirty:false,saveBusy:false,font:{...EMPTY_FONT},fontBusy:false,fontFile:null,fontError:'',fontStatus:'',fontPhase:'',fontProgress:0,fontRevision:0,renderer:null,rendererBusy:false,rendererRefreshBusy:false,rendererError:'',rendererRevision:0,jobs:new Map(),history:[],currentId:null,currentPage:0,previewKey:null,previews:new Map(),groups:[],groupSeq:0,stateBusy:false,jobBusy:false,parseBusy:false,pollTimer:null,jobTimer:null,toastTimer:null,diagnostics:null,view:'overview'};

function setupThemeControls() {
  $('#sample-themes').innerHTML = Object.entries(THEMES).map(([key,theme]) => `<button id="sample-tab-${key}" type="button" role="tab" class="theme-pill${key === DEFAULTS.theme ? ' selected' : ''}" data-sample-theme="${key}" aria-controls="style-sample" aria-selected="${key === DEFAULTS.theme}" tabindex="${key === DEFAULTS.theme ? 0 : -1}">${themeArt(key)}<span>${theme.name}</span></button>`).join('');
  $('#parse-themes').innerHTML = Object.entries(THEMES).map(([key,theme]) => `<label data-theme="${key}"><input type="radio" name="theme" value="${key}" ${key === DEFAULTS.theme ? 'checked' : ''}><span>${themeArt(key)}<strong>${theme.name}</strong><small>${theme.detail}</small></span></label>`).join('');
  $('#parse-font-override').innerHTML = optionsMarkup({options:FONT_OPTIONS},'lxgw');
  selectSampleTheme(DEFAULTS.theme);
}
function selectSampleTheme(key, focus = false) {
  if (!THEMES[key]) return;
  const theme = THEMES[key];
  $$('[data-sample-theme]').forEach(button => {
    const selected = button.dataset.sampleTheme === key;
    button.classList.toggle('selected',selected);
    button.setAttribute('aria-selected',String(selected));
    button.tabIndex = selected ? 0 : -1;
    if (selected && focus) button.focus();
  });
  const sample = $('#style-sample');
  sample.dataset.theme = key;
  sample.setAttribute('aria-labelledby',`sample-tab-${key}`);
  $('#sample-motif').innerHTML = themeArt(key);
  $('#sample-theme-name').textContent = `${theme.name} · ${theme.detail}`;
  $('#sample-theme-description').textContent = theme.description;
  $('#sample-font').textContent = `默认字体：${theme.font}`;
}
function updatePageHeightState() {
  const height = $('#setting-page_height');
  if (!height) return;
  height.disabled = $('#setting-single_image').checked;
  height.closest('.field').classList.toggle('field-inactive',height.disabled);
}
function safeUrl(value) {
  try { const url = new URL(String(value)); return ['http:','https:'].includes(url.protocol) ? url.href : ''; }
  catch { return ''; }
}
function number(value) { return Number.isFinite(Number(value)) ? Number(value).toLocaleString('zh-CN') : '—'; }
function bytes(value) {
  if (!Number.isFinite(Number(value))) return '—';
  const amount = Math.max(0, Number(value));
  if (amount < 1024) return `${number(amount)} B`;
  if (amount < 1024 ** 2) return `${(amount / 1024).toFixed(1)} KB`;
  return `${(amount / 1024 ** 2).toFixed(1)} MB`;
}
function date(value, short = false) {
  if (!value) return '—';
  const parsed = new Date(value);
  if (Number.isNaN(parsed.valueOf())) return String(value);
  const parts = Object.fromEntries(new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hourCycle:'h23'}).formatToParts(parsed).map(part => [part.type,part.value]));
  return `${short ? '' : `${parts.year}-`}${parts.month}-${parts.day} ${parts.hour}:${parts.minute}`;
}
function duration(value) {
  const ms = Number(value);
  return Number.isFinite(ms) && ms >= 0 ? (ms < 1000 ? `${Math.round(ms)} ms` : `${(ms/1000).toFixed(1)} 秒`) : '—';
}
function badge(job) {
  const type = ACTIVE.has(job.status) ? 'active' : (job.status === 'done' ? 'done' : (FAILED.has(job.status) ? 'failed' : 'neutral'));
  return `<span class="status-badge ${type}"><span class="small-dot"></span>${escapeHTML(STATUS[job.status] || job.status || '未知')}</span>`;
}
function toast(message, error = false) {
  const element = $('#toast');
  clearTimeout(S.toastTimer);
  element.textContent = message;
  element.classList.toggle('error', error);
  element.hidden = false;
  S.toastTimer = setTimeout(() => { element.hidden = true; }, error ? 7000 : 4200);
}
function connection(state, message = '') {
  S.online = state === 'online';
  const element = $('#connection-state');
  element.dataset.state = state;
  element.innerHTML = `<span></span>${{online:'已连接',offline:'未连接',connecting:'连接中'}[state]}`;
  $('#reconnect-button').hidden = state !== 'offline';
  $('#connection-banner').hidden = state !== 'offline';
  $('#connection-banner').textContent = message || '尚未连接 AstrBot。请在 AstrBot 插件管理页面打开本控制台。样式示意可查看，真实数据和操作会在连接后启用。';
  $$('[data-online]').forEach(button => { button.disabled = !S.online; });
  $$('[data-retry-job]').forEach(button => { button.disabled = !S.online; });
  $('#parse-submit').disabled = !S.online || S.parseBusy;
  updateFontControls();
  updateRendererControls();
  if (S.currentId) updatePreviewButtons();
}
async function apiGet(endpoint, params = {}) {
  if (!S.bridge) throw new Error('未找到 AstrBot 插件页面桥接，请从 AstrBot 插件管理打开此页面。');
  const result = await S.bridge.apiGet(endpoint, params);
  if (result && result.status === 'error') throw new Error(result.message || result.error || '请求失败');
  return result;
}
async function apiPost(endpoint, body = {}) {
  if (!S.bridge) throw new Error('尚未连接 AstrBot');
  const result = await S.bridge.apiPost(endpoint, body);
  if (result && result.status === 'error') throw new Error(result.message || result.error || '操作失败');
  return result;
}
function rememberJob(job) {
  if (!job?.id) return;
  const old = S.jobs.get(job.id) || {};
  S.jobs.set(job.id, {...old, ...job});
}
function loadedJobs() {
  return S.history.map(id => S.jobs.get(id)).filter(Boolean).sort((a,b) => String(b.started_at || '').localeCompare(String(a.started_at || '')));
}
async function refreshState({quiet = false} = {}) {
  if (S.stateBusy || !S.bridge) return;
  S.stateBusy = true;
  const fontRevision = S.fontRevision;
  const rendererRevision = S.rendererRevision;
  try {
    const data = await apiGet('state');
    if (!data?.settings || !data?.stats) throw new Error('后端返回的数据格式不完整');
    const first = !S.settingsLoaded;
    const previousSettings = JSON.stringify(S.settings);
    const applySettings = !S.fontBusy && fontRevision === S.fontRevision;
    if (applySettings) {
      S.settings = {...DEFAULTS, ...data.settings};
      delete S.settings.cookie;
      if (data.font) S.font = {...EMPTY_FONT,...data.font};
    }
    S.settingsLoaded = true;
    if (data.renderer && !S.rendererBusy && !S.rendererRefreshBusy && rendererRevision === S.rendererRevision) S.renderer = data.renderer;
    (data.jobs || []).forEach(rememberJob);
    (data.history || []).forEach(rememberJob);
    S.history = [...new Set([...(data.jobs || []), ...(data.history || [])].map(job => job.id).filter(Boolean))];
    if (applySettings && (first || (!S.dirty && previousSettings !== JSON.stringify(S.settings)))) populateSettings();
    if (applySettings && first) populateParseOptions();
    renderStats(data.stats);
    renderRecent();
    renderHistory();
    renderActivity();
    renderCache(data.cache || {});
    renderCapabilities(data.capabilities || {});
    connection('online');
    if (S.currentId && S.jobs.has(S.currentId)) updateCurrentJob();
  } catch (error) {
    connection('offline', `连接暂不可用：${error.message}。可使用右上角「重新连接」重试；当前输入会保留。`);
    if (!quiet) toast(error.message, true);
  } finally { S.stateBusy = false; }
}
function renderStats(stats) {
  $$('[data-stat]').forEach(element => { element.textContent = number(stats[element.dataset.stat]); });
  const total = Number(stats.total) || 0, success = Number(stats.success) || 0;
  $('#success-rate').textContent = total ? `成功率 ${Math.round(success / total * 100)}% · 失败 ${number(stats.failed || 0)}` : '等待第一份拾光';
  $('#active-note').textContent = Number(stats.active) ? '后台任务正在进行' : (S.settings.enabled ? '服务已准备就绪' : '自动解析已停用');
  $('#nav-active-count').hidden = !Number(stats.active);
  $('#nav-active-count').textContent = number(stats.active);
}
function renderRecent() {
  const jobs = loadedJobs().slice(0, 5);
  $('#recent-jobs').innerHTML = jobs.length ? jobs.map(job => `<div class="recent-job"><div class="recent-job-icon">${icon(job.kind === 'forum' ? 'grid' : 'image')}</div><div><h3 title="${escapeHTML(job.title || job.input)}">${escapeHTML(job.title || job.input || '等待获取标题')}</h3><p><span>${escapeHTML(date(job.started_at, true))}</span><span>·</span><span>${job.cache_hit ? '缓存命中' : (job.pages ? `${number(job.pages)} 页卡片` : escapeHTML(job.stage || STATUS[job.status] || ''))}</span></p></div><div class="recent-job-action">${badge(job)}<button type="button" class="text-button" data-open-job="${escapeHTML(job.id)}">查看 ${icon('chevron')}</button></div></div>`).join('') : `<div class="empty-state compact">${icon('clock')}<p>还没有任务，粘贴一个链接开始吧。</p></div>`;
}
function renderActivity() {
  const jobs = loadedJobs();
  if (!jobs.length) { $('#activity-chart').innerHTML = '<span class="chart-empty">暂无任务记录</span>'; $('#activity-chart').setAttribute('aria-label', '近七天暂无已加载任务'); return; }
  const days = Array.from({length:7}, (_,index) => {
    const day = new Date(); day.setDate(day.getDate() - 6 + index); day.setHours(0,0,0,0);
    const count = jobs.filter(job => { const t = new Date(job.started_at); return t >= day && t < new Date(day.getTime() + 86400000); }).length;
    return {label:`${day.getMonth()+1}.${day.getDate()}`, count};
  });
  const maximum = Math.max(1, ...days.map(day => day.count));
  $('#activity-chart').innerHTML = days.map(day => `<div class="chart-column" title="${day.label}：${day.count} 个任务"><div class="chart-bar" style="height:${Math.max(3, day.count / maximum * 76)}px"></div><span class="chart-label">${day.label}</span></div>`).join('');
  $('#activity-chart').setAttribute('aria-label', '近七天已加载任务：' + days.map(day => `${day.label}，${day.count} 个`).join('；'));
}
function renderHistory() {
  const filter = $('#history-filter').value, search = $('#history-search').value.trim().toLowerCase();
  const all = loadedJobs();
  const jobs = all.filter(job => (filter === 'all' || (filter === 'active' ? ACTIVE.has(job.status) : (filter === 'failed' ? FAILED.has(job.status) : job.status === filter))) && (!search || `${job.title || ''} ${job.input || ''} ${job.source_url || ''}`.toLowerCase().includes(search)));
  $('#history-count').textContent = `显示 ${number(jobs.length)} / ${number(all.length)} 条已加载记录`;
  $('#history-table').innerHTML = jobs.length ? `<table><thead><tr><th scope="col">内容与来源</th><th scope="col">状态</th><th scope="col">时间 / 耗时</th><th scope="col">结果</th><th scope="col">操作</th></tr></thead><tbody>${jobs.map(job => `<tr><td><div class="history-title" title="${escapeHTML(job.title || job.input)}">${escapeHTML(job.title || job.input || '等待获取标题')}</div><div class="history-subtitle" title="${escapeHTML(job.source_url || job.input)}">${escapeHTML(job.source_url || job.input || job.id)}</div></td><td>${badge(job)}</td><td class="history-time">${escapeHTML(date(job.started_at,true))}<br>${job.cache_hit ? '缓存命中 · ' : ''}${escapeHTML(duration(job.duration_ms))}</td><td>${job.pages ? `${number(job.pages)} 页` : '—'}</td><td><div class="history-actions"><button class="text-button" type="button" data-open-job="${escapeHTML(job.id)}">${resultReady(job) ? '预览' : '详情'}</button>${FAILED.has(job.status) ? `<button class="text-button" type="button" data-retry-job="${escapeHTML(job.id)}" ${S.online ? '' : 'disabled'}>重新解析</button>` : ''}</div></td></tr>`).join('')}</tbody></table>` : `<div class="empty-state">${icon('clock')}<h3>${all.length ? '没有符合条件的记录' : '历史记录还空着'}</h3><p>${all.length ? '试试调整关键词或状态筛选。' : '完成第一份解析后，记录会留在这里。'}</p></div>`;
}
function renderCache(cache) {
  const used = Number(cache.bytes) || 0, maximum = Number(S.settings.max_cache_mb) * 1024 ** 2;
  $('#cache-size').textContent = bytes(used);
  $('#cache-limit').textContent = `/ ${number(S.settings.max_cache_mb)} MB 预算`;
  $('#cache-meter').style.width = `${Math.min(100, maximum ? used / maximum * 100 : 0)}%`;
  $('#cache-entries').textContent = `${number(cache.entries || 0)} 条缓存`;
  $('#cache-policy').textContent = Number(S.settings.cache_minutes) ? `${number(S.settings.cache_minutes)} 分钟有效` : '内容缓存已关闭';
}
function renderCapabilities(capabilities) {
  const labels = {render:'图片渲染',html_render:'HTML 渲染',t2i:'图片渲染',forum:'吧首页解析',thread:'帖子解析',user:'用户主页解析',export:'文件导出',images:'配图',reactions:'贴表情',forward:'合并转发',short_links:'短链接展开'};
  const entries = Object.entries(capabilities);
  $('#capability-list').innerHTML = entries.length ? entries.map(([key,value]) => `<div class="capability-item"><span>${escapeHTML(labels[key] || key)}</span><strong>${typeof value === 'boolean' ? `<span class="status-badge ${value ? 'done' : 'neutral'}">${value ? '可用' : '未启用'}</span>` : escapeHTML(typeof value === 'object' ? JSON.stringify(value) : value)}</strong></div>`).join('') : '<p class="field-help">后端暂未提供能力说明。</p>';
}

function navigate(name, focus = false) {
  if (!VIEWS[name]) name = 'overview';
  S.view = name;
  Object.keys(VIEWS).forEach(key => { $(`#view-${key}`).hidden = key !== name; });
  $$('[data-nav]').forEach(link => { if (link.dataset.nav === name) link.setAttribute('aria-current','page'); else link.removeAttribute('aria-current'); });
  $('#view-title').textContent = VIEWS[name];
  document.title = `${VIEWS[name]} · 贴吧拾光`;
  if (focus) { $('#main-content').focus({preventScroll:true}); window.scrollTo({top:0,behavior:'instant'}); }
}
function populateParseOptions() {
  const form = $('#parse-form');
  const theme = $(`input[name="theme"][value="${S.settings.theme}"]`, form);
  if (theme) theme.checked = true;
  $('#parse-comment-limit').value = S.settings.comment_limit;
  $('#parse-sort').value = S.settings.sort;
  $('#parse-images').checked = S.settings.include_images;
  $('#parse-only-author').checked = S.settings.only_author;
  $('#parse-original-images').checked = Boolean(S.settings.forward_original_images);
  $('#parse-font-override').value = S.settings.font_override;
  $('#parse-single-image').checked = Boolean(S.settings.single_image);
  $('#parse-show-source').checked = Boolean(S.settings.show_source);
}
async function submitParse(event) {
  event.preventDefault();
  if (S.parseBusy || !S.online) return;
  const input = $('#parse-input'), error = $('#parse-input-error');
  const value = input.value.trim();
  error.hidden = true; input.removeAttribute('aria-invalid');
  if (!value) { error.textContent = '请粘贴贴吧链接或分享内容。'; error.hidden = false; input.setAttribute('aria-invalid','true'); input.focus(); return; }
  const limit = $('#parse-comment-limit');
  if (!limit.checkValidity()) { toast('回复楼层数应为 0～100 的整数。',true); limit.focus(); return; }
  const form = $('#parse-form');
  const options = {theme:$('input[name="theme"]:checked',form).value,font_override:$('#parse-font-override').value,single_image:$('#parse-single-image').checked,show_source:$('#parse-show-source').checked,comment_limit:Number(limit.value),sort:$('#parse-sort').value,include_images:$('#parse-images').checked,only_author:$('#parse-only-author').checked,forward_original_images:$('#parse-original-images').checked,output_mode:'card'};
  S.parseBusy = true; $('#parse-submit').disabled = true; $('#parse-submit').innerHTML = `${icon('refresh')}正在提交…`;
  try {
    const data = await apiPost('parse',{input:value,options});
    if (!data?.job?.id) throw new Error('服务未返回任务编号');
    rememberJob(data.job);
    S.history = [...new Set([data.job.id,...S.history])];
    await selectJob(data.job.id, false);
    toast('任务已提交，正在读取内容。');
    refreshState({quiet:true});
  } catch (failure) { error.textContent = failure.message; error.hidden = false; input.setAttribute('aria-invalid','true'); toast(`提交失败：${failure.message}`,true); }
  finally { S.parseBusy = false; $('#parse-submit').disabled = !S.online; $('#parse-submit').innerHTML = `${icon('scan')}开始解析 ${icon('arrow')}`; }
}
async function selectJob(id, changeView = true) {
  if (changeView) { location.hash = 'parse'; navigate('parse',true); }
  S.currentId = id; S.currentPage = 0; S.previewKey = null;
  const known = S.jobs.get(id);
  if (known) updateCurrentJob();
  else $('#current-job').innerHTML = '<p class="field-help">正在读取任务详情…</p>';
  $('#current-job').hidden = false;
  try {
    const data = await apiGet('job',{id});
    if (data?.job && S.currentId === id) { rememberJob(data.job); updateCurrentJob(); }
  } catch (error) { toast(`读取任务失败：${error.message}`,true); }
}
function updateCurrentJob() {
  const job = S.jobs.get(S.currentId);
  if (!job) return;
  const progress = Math.max(0, Math.min(100,Number(job.progress)||0));
  const stageIndex = {queued:0,fetching:1,rendering:2,sending:2,done:3,failed:-1,delivery_failed:2}[job.status];
  const warnings = [...new Set([...(job.warnings || []),...(job.document?.warnings || [])])];
  $('#current-job').hidden = false;
  $('#current-job').innerHTML = `<div class="panel-heading"><h2>${escapeHTML(job.title || '正在准备这份拾光')}</h2>${badge(job)}</div><div class="progress-track" role="progressbar" aria-label="解析进度" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${progress}"><span style="width:${progress}%"></span></div><div class="progress-caption"><span>${escapeHTML(job.stage || STATUS[job.status] || '')}</span><span>${progress}%${job.duration_ms ? ` · ${escapeHTML(duration(job.duration_ms))}` : ''}</span></div><div class="progress-steps">${['等待处理','读取内容','生成卡片','完成'].map((label,index) => `<span class="${stageIndex >= index ? 'complete' : ''}"><i>${stageIndex > index ? '✓' : index + 1}</i>${label}</span>`).join('')}</div>${warnings.map(warning => `<p class="job-warning">${escapeHTML(warning)}</p>`).join('')}${FAILED.has(job.status) ? `<p class="job-error" role="alert">${escapeHTML(job.error || '任务未完成，请查看诊断后重试。')}</p>${job.status === 'delivery_failed' ? '<p class="job-warning">卡片已生成，可预览或下载。重新解析会生成新的面板任务，不会重新发送到 QQ。</p>' : ''}<button class="secondary-button" type="button" data-retry-job="${escapeHTML(job.id)}" ${S.online ? '' : 'disabled'}>${icon('refresh')}重新解析</button>` : ''}`;
  const ready = resultReady(job);
  $('#preview-toolbar').hidden = !ready;
  $('#preview-pages-label').textContent = ready ? `${number(job.pages || 0)} 页 · ${job.cache_hit ? '缓存结果' : '真实解析结果'}` : '完成后显示';
  if (ready) {
    const maximum = Math.max(0,(Number(job.pages)||1)-1);
    S.currentPage = Math.min(S.currentPage,maximum);
    const key = `${job.id}:${S.currentPage}`;
    if (S.previewKey !== key) showPreview();
    const url = safeUrl(job.source_url || job.document?.url);
    $('#document-details').hidden = false;
    $('#document-details').innerHTML = `<div>${escapeHTML(job.document?.title || job.title || '')}</div><div>任务 ${escapeHTML(job.id)} · ${escapeHTML(date(job.finished_at || job.started_at))}</div>${url ? `<a href="${escapeHTML(url)}" target="_blank" rel="noopener noreferrer">${icon('link')}打开原始来源</a>` : '<p>源站地址未提供</p>'}<div>${job.document?.pagination?.has_more ? '当前只收录设置范围内的内容，原页面还有更多内容。' : '内容与统计以本次源站返回为准。'}</div>`;
  } else {
    S.previewKey = null;
    $('#document-details').hidden = true;
    $('#preview-surface').innerHTML = `<div class="empty-state preview-empty">${icon(job.status === 'failed' ? 'info' : 'image')}<h3>${job.status === 'failed' ? '这份拾光还没有生成' : '内容正在路上'}</h3><p>${job.status === 'failed' ? '上方保留了失败原因，可修正后重试。' : '后台完成读取与渲染后，真实卡片会在此显示。'}</p></div>`;
  }
  updatePreviewButtons();
}
function updatePreviewButtons() {
  const job = S.jobs.get(S.currentId);
  const pages = Number(job?.pages)||0;
  $('#preview-prev').disabled = !S.online || S.currentPage <= 0;
  $('#preview-next').disabled = !S.online || S.currentPage >= pages-1;
  $('#preview-page-number').textContent = `${Math.min(S.currentPage+1,pages)} / ${pages}`;
  $('#export-button').disabled = !S.online || !resultReady(job);
}
async function showPreview() {
  const id = S.currentId, page = S.currentPage, key = `${id}:${page}`;
  const job = S.jobs.get(id);
  if (!job || !resultReady(job)) return;
  S.previewKey = key;
  updatePreviewButtons();
  const surface = $('#preview-surface');
  surface.setAttribute('aria-busy','true');
  surface.innerHTML = '<div class="empty-state preview-empty"><p>正在读取第 ' + (page+1) + ' 页卡片…</p></div>';
  try {
    let dataUrl = S.previews.get(key);
    if (!dataUrl) {
      const data = await apiGet('preview',{id,page});
      if (!/^data:image\/(?:png|jpeg|webp);base64,[A-Za-z0-9+/=\r\n]+$/.test(data?.data_url || '')) throw new Error('预览图片格式无效或产物已过期');
      dataUrl = data.data_url; S.previews.set(key,dataUrl);
      while (S.previews.size > 8) S.previews.delete(S.previews.keys().next().value);
    }
    if (S.previewKey !== key) return;
    const image = document.createElement('img');
    image.alt = `${job.title || '贴吧内容'}，第 ${page+1} 页，共 ${job.pages} 页`;
    image.src = dataUrl;
    surface.replaceChildren(image);
  } catch (error) {
    if (S.previewKey !== key) return;
    S.previewKey = null;
    surface.innerHTML = `<div class="empty-state preview-empty">${icon('info')}<h3>预览暂时不可用</h3><p>${escapeHTML(error.message)}</p><button class="text-button" type="button" id="retry-preview">重新读取</button></div>`;
  } finally { if (S.currentId === id && S.currentPage === page) surface.removeAttribute('aria-busy'); }
}
async function retryJob(id, button) {
  if (!S.online) return;
  if (button) button.disabled = true;
  try {
    const data = await apiPost('retry',{id});
    if (!data?.job?.id) throw new Error('服务未返回新的任务');
    rememberJob(data.job); S.history = [...new Set([data.job.id,...S.history])];
    await selectJob(data.job.id); toast('已重新提交解析任务。'); refreshState({quiet:true});
  } catch (error) { toast(`重试失败：${error.message}`,true); }
  finally { if (button?.isConnected) button.disabled = !S.online; }
}
async function exportJob() {
  const job = S.jobs.get(S.currentId);
  if (!job || !resultReady(job) || !S.online) return;
  const format = $('#export-format').value, page = S.currentPage;
  const extension = format === 'markdown' ? 'md' : format;
  $('#export-button').disabled = true;
  try {
    await S.bridge.download('export',{id:job.id,format,page},`tieba-${job.id}${format === 'png' ? `-${page+1}` : ''}.${extension}`);
    toast('导出已交给浏览器下载。');
  } catch (error) { toast(`导出失败：${error.message}`,true); }
  finally { updatePreviewButtons(); }
}

function optionsMarkup(field, value) {
  return Object.entries(field.options).map(([key,label]) => `<option value="${escapeHTML(key)}" ${String(value) === key ? 'selected' : ''}>${escapeHTML(label)}</option>`).join('');
}
function fieldMarkup(field) {
  const id = `setting-${field.key}`, value = S.settings[field.key] ?? DEFAULTS[field.key];
  if (field.type === 'boolean') return `<div class="toggle-row"><label for="${id}"><strong>${escapeHTML(field.label)}</strong><small id="${id}-help">${escapeHTML(field.help)}</small></label><input class="switch" id="${id}" name="${field.key}" type="checkbox" aria-describedby="${id}-help" ${value ? 'checked' : ''}></div>`;
  const input = field.type === 'select' ? `<select id="${id}" name="${field.key}" aria-describedby="${id}-help">${optionsMarkup(field,value)}</select>` : `<input id="${id}" name="${field.key}" type="${field.type === 'number' ? 'number' : 'text'}" value="${escapeHTML(value)}" ${field.type === 'number' ? `min="${field.min}" max="${field.max}" step="${field.step || 1}"` : 'inputmode="numeric" pattern="[0-9]{1,7}" maxlength="7"'} required aria-describedby="${id}-help ${id}-error">`;
  return `<div class="field"><label class="field-label" for="${id}">${escapeHTML(field.label)}</label>${input}<p class="field-help" id="${id}-help">${escapeHTML(field.help)}</p><p class="field-error" id="${id}-error" hidden></p></div>`;
}
function rendererManagementMarkup() {
  return `<div class="renderer-management" role="group" aria-labelledby="renderer-management-title"><div class="form-divider"></div><div class="renderer-heading"><h3 id="renderer-management-title">渲染环境</h3><button id="renderer-refresh" class="secondary-button" type="button" disabled>${icon('refresh')}刷新检测</button></div><dl class="renderer-detection"><div><dt>当前使用</dt><dd id="renderer-active">连接后检测</dd></div><div><dt>AstrBot 服务</dt><dd id="renderer-service">连接后检测</dd></div><div><dt>本地浏览器</dt><dd id="renderer-local">连接后检测</dd></div></dl><p class="field-help renderer-guidance">已有可用渲染服务时可直接复用。需要本地出图时，在下方准备浏览器，再选择渲染方式并保存。</p><div class="renderer-engines">${Object.entries(BROWSER_ENGINES).map(([id,name]) => `<article class="renderer-engine"><div class="renderer-engine-description"><h4>${escapeHTML(name)}${id === 'headless' ? '<span class="renderer-recommended">低占用推荐</span>' : ''}</h4><p class="field-help">${id === 'headless' ? '专用于后台生成卡片，下载与存储占用较低。' : '包含完整浏览器，下载体积和存储占用较高。'}</p><span class="renderer-installed" id="renderer-installed-${id}">等待检测</span></div><button class="secondary-button" type="button" data-renderer-prepare="${id}" disabled>下载准备</button></article>`).join('')}</div><div class="renderer-install" aria-live="polite" role="status"><span class="status-badge neutral" id="renderer-install-state">等待检测</span><p class="field-help" id="renderer-install-stage">连接后读取浏览器准备状态。</p><progress id="renderer-install-progress" aria-label="浏览器准备进度" hidden></progress></div><p class="field-error" id="renderer-error" role="alert" hidden></p></div>`;
}
function updateRendererControls() {
  const refresh = $('#renderer-refresh');
  if (!refresh) return;
  const setText = (element, value) => { if (element.textContent !== value) element.textContent = value; };
  const renderer = S.renderer, install = renderer?.install || {};
  const running = install.status === 'running';
  const requestBusy = S.rendererBusy || S.rendererRefreshBusy;
  refresh.disabled = !S.online || requestBusy;
  refresh.innerHTML = `${icon('refresh')}${S.rendererRefreshBusy ? '正在检测…' : '刷新检测'}`;
  $('#renderer-active').textContent = renderer ? ({service:'AstrBot 渲染服务',local:'本地浏览器',pending:'等待可用环境'}[renderer.active] || '等待可用环境') : '连接后检测';
  $('#renderer-service').textContent = !renderer ? '连接后检测' : (renderer.service?.available ? (renderer.service.configured ? '已配置，可用' : '可用') : (renderer.service?.configured ? '已配置，暂不可用' : '尚未配置'));
  $('#renderer-local').textContent = !renderer ? '连接后检测' : (renderer.local?.available ? `可用${renderer.local.browser ? ' · ' + renderer.local.browser : ''}` : '暂未检测到可用浏览器');
  for (const id of Object.keys(BROWSER_ENGINES)) {
    const engine = renderer?.engines?.find(item => item.id === id);
    const installed = Boolean(engine?.installed);
    setText($(`#renderer-installed-${id}`),!renderer ? '等待检测' : (installed ? '已准备' : '尚未准备'));
    $(`#renderer-installed-${id}`).classList.toggle('ready',installed);
    const button = $(`[data-renderer-prepare="${id}"]`);
    button.disabled = !S.online || requestBusy || running || S.saveBusy;
    button.textContent = running && install.engine === id ? '正在准备…' : (installed ? '检查准备' : (install.status === 'failed' && install.engine === id ? '重新准备' : '下载准备'));
    button.setAttribute('aria-label',`${button.textContent}${BROWSER_ENGINES[id]}`);
  }
  const badge = $('#renderer-install-state');
  setText(badge,S.rendererBusy ? '提交中' : ({idle:'等待准备',running:'后台准备中',done:'已准备',failed:'准备失败'}[install.status] || '等待检测'));
  badge.className = `status-badge ${install.status === 'failed' ? 'failed' : (running || S.rendererBusy ? 'active' : (install.status === 'done' ? 'done' : 'neutral'))}`;
  const name = BROWSER_ENGINES[install.engine] || '浏览器';
  setText($('#renderer-install-stage'),S.rendererBusy ? '正在提交浏览器准备任务…' : (running ? `${name}：${install.stage || '正在后台处理，可继续使用面板。'}` : (install.status === 'done' ? `${name}已准备。选择本地浏览器并保存后即可使用。` : (install.status === 'failed' ? `${name}准备失败，可重新准备。` : (renderer ? '按需下载；精简 Chromium 适合日常出图。' : '连接后读取浏览器准备状态。')))));
  $('#renderer-install-progress').hidden = !running && !S.rendererBusy;
  $('.renderer-management').setAttribute('aria-busy',String(running || S.rendererBusy));
  const error = S.rendererError || (!S.rendererBusy && install.status === 'failed' ? install.error : '') || '';
  setText($('#renderer-error'),error);
  $('#renderer-error').hidden = !error;
}
async function refreshRenderer() {
  if (!S.online || S.rendererBusy || S.rendererRefreshBusy) return;
  S.rendererRefreshBusy = true; S.rendererError = ''; S.rendererRevision++;
  updateRendererControls();
  try {
    const data = await apiGet('renderer');
    if (!data?.renderer) throw new Error('后端未返回渲染环境，请刷新插件后重试。');
    S.renderer = data.renderer;
  } catch (error) { S.rendererError = `环境检测失败：${error.message}`; }
  finally { S.rendererRefreshBusy = false; S.rendererRevision++; updateRendererControls(); }
}
async function prepareRenderer(engine) {
  if (!Object.hasOwn(BROWSER_ENGINES,engine) || !S.online || S.rendererBusy || S.rendererRefreshBusy || S.saveBusy || S.renderer?.install?.status === 'running') return;
  S.rendererBusy = true; S.rendererError = ''; S.rendererRevision++;
  updateRendererControls();
  try {
    const data = await apiPost('renderer/prepare',{engine});
    if (!data?.renderer) throw new Error('后端未返回浏览器准备状态，请刷新检测。');
    S.renderer = data.renderer;
  } catch (error) { S.rendererError = `无法准备浏览器：${error.message}`; }
  finally { S.rendererBusy = false; S.rendererRevision++; updateRendererControls(); }
}
function fontUploadMarkup() {
  return `<div class="font-upload" role="group" aria-labelledby="font-upload-title"><div class="form-divider"></div><h3 id="font-upload-title">自定义字体</h3><p id="font-upload-help" class="field-help">支持 TTF、OTF、WOFF、WOFF2，最大 32 MB。上传并应用后，全局卡片使用该字体；分群与本次解析也可单独选择。未上传时使用霞鹜文楷。</p><p id="font-current" class="font-current"></p><label class="field-label" for="font-file">字体文件</label><input id="font-file" class="sr-only" tabindex="-1" type="file" accept=".ttf,.otf,.woff,.woff2" aria-describedby="font-upload-help font-selected font-upload-error"><button id="font-choose-action" class="secondary-button" type="button" aria-controls="font-file" disabled>选择字体文件</button><p id="font-selected" class="field-help"></p><div class="font-upload-actions"><button id="font-upload-action" class="secondary-button" type="button" disabled>上传并应用</button><button id="font-reset-action" class="text-button" type="button" disabled>恢复霞鹜文楷</button></div><progress id="font-upload-progress" max="100" value="0" aria-label="字体处理进度" hidden></progress><p id="font-upload-status" class="field-help" role="status" aria-live="polite"></p><p id="font-upload-error" class="field-error" role="alert" hidden></p></div>`;
}
function updateFontControls() {
  const file = $('#font-file');
  if (!file) return;
  const busy = S.fontBusy || S.saveBusy;
  file.disabled = !S.online || busy;
  $('#font-choose-action').disabled = !S.online || busy;
  file.setAttribute('aria-invalid',String(Boolean(S.fontError)));
  $('#font-current').textContent = S.font.uploaded ? `已上传：${S.font.name} · ${(Number(S.font.size) / 1024 / 1024).toFixed(2)} MB` : '尚未上传自定义字体，当前可使用霞鹜文楷。';
  $('#font-selected').textContent = S.fontFile ? `待上传：${S.fontFile.name} · ${(S.fontFile.size / 1024 / 1024).toFixed(2)} MB` : '选择文件后点击「上传并应用」。';
  $('#font-upload-action').disabled = !S.online || busy || !S.fontFile;
  $('#font-upload-action').textContent = S.fontBusy && S.fontPhase !== 'resetting' ? '正在处理字体…' : (S.font.uploaded ? '更换并应用' : '上传并应用');
  $('#font-reset-action').disabled = !S.online || busy || (!S.font.uploaded && S.settings.font_override === 'lxgw');
  $('#font-reset-action').textContent = S.fontBusy && S.fontPhase === 'resetting' ? '正在恢复…' : '恢复霞鹜文楷';
  $('#font-upload-error').textContent = S.fontError;
  $('#font-upload-error').hidden = !S.fontError;
  $('#font-upload-status').textContent = S.fontStatus;
  const progress = $('#font-upload-progress');
  progress.hidden = !S.fontBusy;
  if (S.fontPhase === 'reading' || S.fontPhase === 'uploading') progress.value = S.fontProgress;
  else progress.removeAttribute('value');
  $('#settings-save').disabled = !S.online || busy;
  $('#settings-reset').disabled = !S.online || busy;
  updateRendererControls();
}
function validateFontFile(file) {
  if (!file) throw new Error('请先选择字体文件。');
  if (!/\.(?:ttf|otf|woff|woff2)$/i.test(file.name)) throw new Error('请选择 TTF、OTF、WOFF 或 WOFF2 字体文件。');
  if (!file.size) throw new Error('字体文件为空，请重新选择。');
  if (file.size > FONT_LIMIT) throw new Error('字体文件不能超过 32 MB，请选择较小的字体文件。');
}
function readFontFile(file) {
  return new Promise((resolve,reject) => {
    const reader = new FileReader();
    reader.onprogress = event => {
      if (event.lengthComputable) {
        S.fontProgress = Math.round(event.loaded / event.total * 100);
        S.fontStatus = `正在读取字体文件… ${S.fontProgress}%`;
        updateFontControls();
      }
    };
    reader.onerror = () => reject(new Error('无法读取字体文件，请重新选择后再试。'));
    reader.onabort = () => reject(new Error('字体文件读取已取消，请重新选择后再试。'));
    reader.onload = () => {
      const encoded = String(reader.result || '').split(',')[1];
      if (!encoded) reject(new Error('字体文件读取失败，请重新选择后再试。'));
      else resolve(encoded);
    };
    reader.readAsDataURL(file);
  });
}
function applyFontResult(data) {
  if (!data?.font || !data?.settings) throw new Error('后端未返回完整字体状态，请刷新面板查看结果。');
  S.font = {...EMPTY_FONT,...data.font};
  S.settings = {...DEFAULTS,...data.settings};
  delete S.settings.cookie;
  S.fontFile = null;
  S.fontRevision++;
  $('#setting-font_override').value = S.settings.font_override;
  $('#font-file').value = '';
  $('#parse-font-override').value = S.settings.font_override;
}
async function uploadFont() {
  if (!S.online || S.fontBusy || S.saveBusy) return;
  S.fontError = '';
  try { validateFontFile(S.fontFile); }
  catch (error) { S.fontError = error.message; updateFontControls(); return; }
  S.fontBusy = true; S.fontPhase = 'reading'; S.fontProgress = 0; S.fontRevision++;
  S.fontStatus = '正在读取字体文件…'; updateFontControls();
  try {
    const file = S.fontFile, name = file.name;
    const data = await readFontFile(file);
    let offset = 0, uploadId;
    S.fontPhase = 'uploading'; S.fontProgress = 0;
    S.fontStatus = `正在上传字体… 已上传 0 / ${number(file.size)} 字节（0%）`; updateFontControls();
    for (let index = 0; index < data.length; index += FONT_CHUNK_CHARACTERS) {
      const chunk = data.slice(index,index + FONT_CHUNK_CHARACTERS);
      const bytes = chunk.length / 4 * 3 - (chunk.endsWith('==') ? 2 : (chunk.endsWith('=') ? 1 : 0));
      const expected = offset + bytes, last = index + chunk.length === data.length;
      if (!Number.isInteger(bytes) || bytes <= 0 || expected > file.size || (last && expected !== file.size)) throw new Error('字体分块数据异常，请重新选择文件。');
      const payload = {name,data:chunk,offset,total_size:file.size};
      if (uploadId) payload.upload_id = uploadId;
      const response = await apiPost('font',payload);
      if (last) {
        if (response?.complete !== true || (response.received !== undefined && response.received !== expected)) throw new Error('服务器未确认完整字体，请重新上传。');
        applyFontResult(response);
        offset = expected;
      } else {
        if (response?.complete !== false || !/^[0-9a-f]{32}$/.test(response.upload_id || '') || response.received !== expected || (uploadId && response.upload_id !== uploadId)) throw new Error('字体上传分块顺序异常，请重新上传。');
        uploadId = response.upload_id;
        offset = response.received;
      }
      S.fontProgress = Math.floor(offset / file.size * 100);
      S.fontStatus = `正在上传字体… 已上传 ${number(offset)} / ${number(file.size)} 字节（${S.fontProgress}%）`; updateFontControls();
    }
    S.fontStatus = '字体已上传并应用，后续解析将使用新字体。';
    toast('自定义字体已上传并应用。');
  } catch (error) { S.fontError = `上传失败：${error.message}`; S.fontStatus = ''; }
  finally { S.fontBusy = false; S.fontPhase = ''; updateFontControls(); }
}
async function resetFont() {
  if (!S.online || S.fontBusy || S.saveBusy) return;
  S.fontBusy = true; S.fontPhase = 'resetting'; S.fontError = ''; S.fontRevision++;
  S.fontStatus = '正在恢复霞鹜文楷…'; updateFontControls();
  try {
    applyFontResult(await apiPost('font/reset'));
    S.fontStatus = '已恢复霞鹜文楷，并移除已上传的自定义字体。';
    toast('已恢复霞鹜文楷。');
  } catch (error) { S.fontError = `恢复失败：${error.message}`; S.fontStatus = ''; }
  finally { S.fontBusy = false; S.fontPhase = ''; updateFontControls(); }
}
function populateSettings() {
  $('#settings-fields').innerHTML = SECTIONS.map((section,index) => `<article class="panel settings-section" id="settings-${section.id}"><div class="section-number">${String(index+1).padStart(2,'0')}</div><div class="panel-heading"><div><h2>${escapeHTML(section.title)}</h2><p class="field-help">${escapeHTML(section.description)}</p></div></div><div class="settings-fields">${section.fields.map(fieldMarkup).join('')}</div>${section.id === 'render' ? fontUploadMarkup() : (section.id === 'renderer' ? rendererManagementMarkup() : '')}</article>`).join('');
  $('#settings-groups .section-number').textContent = String(SECTIONS.length + 1).padStart(2,'0');
  $('#settings-cookie .section-number').textContent = String(SECTIONS.length + 2).padStart(2,'0');
  $('#settings-nav').innerHTML = [...SECTIONS.map(section => [section.id,section.title]),['groups','分群与黑名单'],['cookie','源站凭据']].map(([id,title],index) => `<a href="#settings-${id}" data-setting-anchor="${id}"><span>${String(index+1).padStart(2,'0')}</span>${escapeHTML(title)}</a>`).join('');
  $('#setting-blacklist').value = (S.settings.blacklist || []).join('\n');
  $('#setting-cookie').value = ''; $('#clear-cookie').checked = false; $('#setting-cookie').disabled = false;
  $('#cookie-status').textContent = S.settings.cookie_set ? '已保存凭据' : '未设置';
  $('#cookie-status').className = `status-badge ${S.settings.cookie_set ? 'done' : 'neutral'}`;
  S.groups = Object.entries(S.settings.group_overrides || {}).map(([id,stored]) => {
    const entries = Object.entries(stored && typeof stored === 'object' ? stored : {});
    const supported = key => GROUP_KEYS.includes(key) && FIELDS[key];
    return {uid:++S.groupSeq,id,values:Object.fromEntries(entries.filter(([key]) => supported(key))),ignored:entries.filter(([key]) => !supported(key)).length};
  });
  renderOverrides();
  updatePageHeightState();
  markDirty(false);
  $('#settings-errors').hidden = true;
  updateFontControls();
}
function markDirty(value = true) {
  S.dirty = value;
  $('#unsaved-dot').hidden = !value;
  $('#settings-save-status').textContent = !S.settingsLoaded ? '等待加载' : (value ? '有未保存的更改' : '已与后端同步');
  $('#settings-save-status').classList.toggle('dirty',value);
  $('#savebar-text').textContent = value ? '更改尚未保存' : '所有变更统一保存';
  $('#savebar-dot').style.color = value ? '#a58542' : 'var(--muted)';
}
function overrideControl(field, value, uid, key) {
  if (!field) return '';
  const label = `${field.label}的群聊覆盖值`;
  if (field.type === 'boolean') return `<select data-override-value="${uid}" data-key="${key}" aria-label="${escapeHTML(label)}"><option value="true" ${value ? 'selected' : ''}>开启</option><option value="false" ${!value ? 'selected' : ''}>关闭</option></select>`;
  if (field.type === 'select') return `<select data-override-value="${uid}" data-key="${key}" aria-label="${escapeHTML(label)}">${optionsMarkup(field,value)}</select>`;
  return `<input data-override-value="${uid}" data-key="${key}" type="number" value="${escapeHTML(value)}" min="${field.min}" max="${field.max}" step="${field.step || 1}" required aria-label="${escapeHTML(label)}">`;
}
function renderOverrides() {
  $('#group-overrides').innerHTML = S.groups.length ? S.groups.map(group => {
    const entries = Object.entries(group.values).filter(([key]) => GROUP_KEYS.includes(key) && FIELDS[key]);
    const rows = entries.map(([key,value]) => `<div class="override-row"><select data-override-key="${group.uid}" data-old-key="${key}" aria-label="要覆盖的设置项">${GROUP_KEYS.filter(candidate => candidate === key || !(candidate in group.values)).map(candidate => `<option value="${candidate}" ${candidate === key ? 'selected' : ''}>${escapeHTML(FIELDS[candidate].label)}</option>`).join('')}</select>${overrideControl(FIELDS[key],value,group.uid,key)}<button class="icon-button" type="button" data-remove-override="${group.uid}" data-key="${key}" aria-label="移除${escapeHTML(FIELDS[key].label)}覆盖">${icon('trash')}</button></div>`).join('');
    return `<div class="group-card" data-group="${group.uid}"><div class="group-card-heading"><label for="group-id-${group.uid}">群号<input id="group-id-${group.uid}" data-group-id="${group.uid}" type="text" inputmode="numeric" pattern="[1-9][0-9]{3,19}" maxlength="20" placeholder="输入群号" value="${escapeHTML(group.id)}" required></label><button class="icon-button" type="button" data-remove-group="${group.uid}" aria-label="移除群聊覆盖 ${escapeHTML(group.id || '新群聊')}">${icon('trash')}</button></div>${rows}<button class="text-button add-override" type="button" data-add-override="${group.uid}" ${entries.length === GROUP_KEYS.length ? 'disabled' : ''}>+ 添加覆盖项</button><p class="field-help">${entries.length ? '未添加的选项继续跟随全局设置。' : '尚未覆盖任何选项，当前全部跟随全局。'}</p>${group.ignored ? `<p class="field-help legacy-note">已忽略 ${group.ignored} 项不再支持的旧设置，保存后会清理。</p>` : ''}</div>`;
  }).join('') : '<div class="group-empty">为指定群聊单独选择主题、字体与长图方式。</div>';
}
function showSettingsErrors(errors) {
  const box = $('#settings-errors');
  box.innerHTML = `<h2>有 ${errors.length} 项需要调整</h2><ul>${errors.map(error => `<li><a href="#${escapeHTML(error.id)}" data-error-target="${escapeHTML(error.id)}">${escapeHTML(error.message)}</a></li>`).join('')}</ul>`;
  box.hidden = false; box.focus();
}
function collectSettings() {
  const result = {}, errors = [];
  $$('[aria-invalid=true]', $('#settings-form')).forEach(input => input.removeAttribute('aria-invalid'));
  $$('.field-error',$('#settings-form')).forEach(element => { element.hidden = true; });
  Object.values(FIELDS).forEach(field => {
    const input = $(`#setting-${field.key}`);
    if (!input.checkValidity()) {
      const message = field.type === 'reaction' ? `${field.label}应为 1～7 位数字。` : `${field.label}应在 ${field.min}～${field.max} 之间${field.step ? '。' : '，且为整数。'}`;
      input.setAttribute('aria-invalid','true');
      const error = $(`#setting-${field.key}-error`);
      if (error) { error.textContent = message; error.hidden = false; }
      errors.push({id:input.id,message});
    }
    result[field.key] = field.type === 'boolean' ? input.checked : (field.type === 'number' ? Number(input.value) : input.value);
  });
  result.blacklist = [...new Set($('#setting-blacklist').value.split(/[\s,，;；]+/).map(value => value.trim()).filter(Boolean))];
  if (result.blacklist.length > 1000 || result.blacklist.some(value => !/^[1-9][0-9]{3,19}$/.test(value))) {
    $('#setting-blacklist').setAttribute('aria-invalid','true'); errors.push({id:'setting-blacklist',message:'黑名单群号应为 4～20 位正整数，最多 1000 个。'});
  }
  result.group_overrides = {};
  if (S.groups.length > 500) errors.push({id:'settings-groups',message:'分群覆盖最多支持 500 个群聊。'});
  S.groups.forEach(group => {
    const input = $(`#group-id-${group.uid}`), id = input.value.trim();
    if (!/^[1-9][0-9]{3,19}$/.test(id) || Object.hasOwn(result.group_overrides,id)) {
      input.setAttribute('aria-invalid','true'); errors.push({id:input.id,message:`${id || '新群聊'}：群号应为 4～20 位正整数，且不能重复。`});
    }
    const values = {};
    Object.keys(group.values).filter(key => GROUP_KEYS.includes(key) && FIELDS[key]).forEach(key => {
      const control = $(`[data-override-value="${group.uid}"][data-key="${key}"]`);
      if (!control.checkValidity()) { control.setAttribute('aria-invalid','true'); errors.push({id:input.id,message:`群 ${id} 的${FIELDS[key].label}超出允许范围。`}); }
      values[key] = FIELDS[key].type === 'boolean' ? control.value === 'true' : (FIELDS[key].type === 'number' ? Number(control.value) : control.value);
    });
    result.group_overrides[id] = values;
  });
  const cookie = $('#setting-cookie').value.trim();
  if ($('#clear-cookie').checked) result.cookie = '';
  else if (cookie) {
    if (cookie.length > 16384 || /[\r\n\0]/.test(cookie)) errors.push({id:'setting-cookie',message:'Cookie 应为单行内容，且不超过 16384 字符。'});
    else result.cookie = cookie;
  }
  return {result,errors};
}
async function saveSettings(event) {
  event.preventDefault();
  if (!S.online || S.fontBusy || S.saveBusy) return;
  const {result,errors} = collectSettings();
  if (errors.length) { showSettingsErrors(errors); return; }
  $('#settings-errors').hidden = true;
  S.saveBusy = true; updateFontControls();
  $('#settings-save').disabled = true; $('#settings-save').textContent = '正在保存…';
  try {
    const data = await apiPost('settings',result);
    if (!data?.settings) throw new Error('后端未返回保存后的设置');
    S.settings = {...DEFAULTS,...data.settings}; delete S.settings.cookie;
    populateSettings();
    populateParseOptions();
    toast('设置已保存，后续任务将使用新的偏好。');
    refreshState({quiet:true});
  } catch (error) { showSettingsErrors([{id:'settings-save',message:`保存失败：${error.message}`}]); toast(`保存失败：${error.message}`,true); }
  finally { S.saveBusy = false; $('#settings-save').innerHTML = `${icon('check')}保存设置`; updateFontControls(); }
}

function confirmAction(title, description, accept = '确认清理') {
  return new Promise(resolve => {
    const dialog = $('#confirm-dialog');
    $('#confirm-title').textContent = title; $('#confirm-description').textContent = description; $('#confirm-accept').textContent = accept;
    dialog.addEventListener('close', () => resolve(dialog.returnValue === 'confirm'),{once:true});
    dialog.showModal();
    $('.secondary-button',dialog).focus();
  });
}
async function clearResource(type) {
  if (!S.online) return;
  const history = type === 'history';
  const approved = await confirmAction(history ? '清空已完成任务记录？' : '清理内容缓存？', history ? '后台将清理已完成或失败的历史任务。正在处理的任务会保留。清理后旧结果可能无法继续预览或导出。' : '近期解析的内容缓存将被清理，后续访问会重新读取源站。正在处理的任务不会被取消。');
  if (!approved) return;
  const button = $(`#${type}-clear`); button.disabled = true;
  try {
    const result = await apiPost(`${type}/clear`);
    if (history) {
      S.previews.clear();
      if (S.currentId && !ACTIVE.has(S.jobs.get(S.currentId)?.status)) { S.currentId = null; S.previewKey = null; $('#current-job').hidden = true; $('#preview-toolbar').hidden = true; $('#document-details').hidden = true; $('#preview-surface').innerHTML = '<div class="empty-state preview-empty"><h3>任务记录已清理</h3><p>重新解析内容后，可在这里查看新的卡片。</p></div>'; }
    }
    toast(`已清理 ${number(result?.cleared || 0)} ${history ? '条任务记录' : '条缓存'}。`); await refreshState({quiet:true});
  } catch (error) { toast(`清理失败：${error.message}`,true); }
  finally { button.disabled = !S.online; }
}
function redactDiagnostics(value, key = '') {
  if (/cookie|authorization|password|secret|token/i.test(key) && !/_set$/.test(key)) return value ? '[已隐藏]' : '';
  if (Array.isArray(value)) return value.map(item => redactDiagnostics(item));
  if (value && typeof value === 'object') return Object.fromEntries(Object.entries(value).map(([k,v]) => [k,redactDiagnostics(v,k)]));
  return value;
}
async function diagnostics() {
  $('#diagnostics-refresh').disabled = true;
  try {
    const data = await apiGet('diagnostics');
    S.diagnostics = redactDiagnostics(data);
    $('#diagnostics-output').textContent = JSON.stringify(S.diagnostics,null,2);
    $('#diagnostics-copy').disabled = false;
    if (data.plugin_version) $('#plugin-version').textContent = String(data.plugin_version);
    toast('已读取当前运行诊断。');
  } catch (error) { $('#diagnostics-output').textContent = `诊断读取失败：${error.message}`; toast(error.message,true); }
  finally { $('#diagnostics-refresh').disabled = !S.online; }
}

function bindEvents() {
  window.addEventListener('hashchange', () => {
    const name = location.hash.slice(1);
    if (VIEWS[name]) navigate(name,true);
  });
  document.addEventListener('click', event => {
    const open = event.target.closest('[data-open-job]');
    if (open) { selectJob(open.dataset.openJob); return; }
    const retry = event.target.closest('[data-retry-job]');
    if (retry) { retryJob(retry.dataset.retryJob,retry); return; }
    const anchor = event.target.closest('[data-setting-anchor]');
    if (anchor) { event.preventDefault(); $(`#settings-${anchor.dataset.settingAnchor}`).scrollIntoView({behavior:matchMedia('(prefers-reduced-motion:reduce)').matches ? 'instant' : 'smooth',block:'start'}); return; }
    const target = event.target.closest('[data-error-target]');
    if (target) { event.preventDefault(); const input = document.getElementById(target.dataset.errorTarget); input?.focus(); input?.scrollIntoView({block:'center'}); }
    if (event.target.closest('#retry-preview')) showPreview();
  });
  $('#color-mode').addEventListener('click', () => {
    const dark = document.documentElement.dataset.colorMode !== 'dark';
    document.documentElement.dataset.colorMode = dark ? 'dark' : 'light';
    $('#color-mode').setAttribute('aria-label', dark ? '切换浅色模式' : '切换深色模式');
    try { localStorage.setItem('tieba-lens-color-mode',dark ? 'dark' : 'light'); } catch {}
  });
  $('#sample-themes').addEventListener('click',event => {
    const button = event.target.closest('[data-sample-theme]');
    if (button) selectSampleTheme(button.dataset.sampleTheme);
  });
  $('#sample-themes').addEventListener('keydown',event => {
    const button = event.target.closest('[data-sample-theme]');
    if (!button || !['ArrowRight','ArrowLeft','ArrowDown','ArrowUp','Home','End'].includes(event.key)) return;
    const keys = Object.keys(THEMES), index = keys.indexOf(button.dataset.sampleTheme);
    const next = event.key === 'Home' ? 0 : event.key === 'End' ? keys.length - 1 : (index + (['ArrowRight','ArrowDown'].includes(event.key) ? 1 : -1) + keys.length) % keys.length;
    event.preventDefault(); selectSampleTheme(keys[next],true);
  });
  $('#quick-form').addEventListener('submit', event => { event.preventDefault(); $('#parse-input').value = $('#quick-input').value; location.hash = 'parse'; navigate('parse',true); $('#parse-input').focus(); });
  $('#parse-form').addEventListener('submit',submitParse);
  $('#preview-prev').addEventListener('click', () => { if (S.currentPage > 0) { S.currentPage--; showPreview(); } });
  $('#preview-next').addEventListener('click', () => { if (S.currentPage < Number(S.jobs.get(S.currentId)?.pages)-1) { S.currentPage++; showPreview(); } });
  $('#export-button').addEventListener('click',exportJob);
  $('#history-search').addEventListener('input',renderHistory);
  $('#history-filter').addEventListener('change',renderHistory);
  $('#history-refresh').addEventListener('click',async () => { await refreshState(); if (S.online) toast('任务记录已刷新。'); });
  $('#history-clear').addEventListener('click',() => clearResource('history'));
  $('#cache-clear').addEventListener('click',() => clearResource('cache'));
  $('#diagnostics-refresh').addEventListener('click',diagnostics);
  $('#diagnostics-copy').addEventListener('click',async () => { try { await navigator.clipboard.writeText(JSON.stringify(S.diagnostics,null,2)); toast('诊断信息已复制。'); } catch { toast('浏览器未允许复制，请在诊断区域选择文本后复制。',true); $('#diagnostics-output').focus(); } });
  $('#settings-form').addEventListener('input', event => {
    if (event.target.id === 'font-file') return;
    if (event.target.matches('[data-group-id]')) {
      const group = S.groups.find(group => group.uid === Number(event.target.dataset.groupId)); if (group) group.id = event.target.value;
    }
    if (event.target.matches('[data-override-value]')) {
      const group = S.groups.find(group => group.uid === Number(event.target.dataset.overrideValue));
      const field = FIELDS[event.target.dataset.key];
      if (group && field) group.values[field.key] = field.type === 'boolean' ? event.target.value === 'true' : (field.type === 'number' ? Number(event.target.value) : event.target.value);
    }
    markDirty();
  });
  $('#settings-form').addEventListener('change', event => {
    if (event.target.id === 'font-file') return;
    if (event.target.id === 'setting-single_image') updatePageHeightState();
    if (event.target.id === 'clear-cookie') { $('#setting-cookie').disabled = event.target.checked; if (event.target.checked) $('#setting-cookie').value = ''; }
    if (event.target.matches('[data-override-key]')) {
      const group = S.groups.find(group => group.uid === Number(event.target.dataset.overrideKey));
      const old = event.target.dataset.oldKey, key = event.target.value;
      if (group && old !== key) { delete group.values[old]; group.values[key] = S.settings[key] ?? DEFAULTS[key]; renderOverrides(); $(`[data-override-value="${group.uid}"][data-key="${key}"]`)?.focus(); }
    }
    // Select and checkbox input is also reported by modern browsers, but change
    // keeps the editor reliable in embedded WebViews.
    if (event.target.matches('[data-override-value]')) {
      const group = S.groups.find(group => group.uid === Number(event.target.dataset.overrideValue));
      const field = FIELDS[event.target.dataset.key];
      if (group && field) group.values[field.key] = field.type === 'boolean' ? event.target.value === 'true' : (field.type === 'number' ? Number(event.target.value) : event.target.value);
    }
    markDirty();
  });
  $('#group-overrides').addEventListener('click',event => {
    const removeGroup = event.target.closest('[data-remove-group]');
    if (removeGroup) { S.groups = S.groups.filter(group => group.uid !== Number(removeGroup.dataset.removeGroup)); renderOverrides(); markDirty(); $('#add-group').focus(); return; }
    const remove = event.target.closest('[data-remove-override]');
    if (remove) { const group = S.groups.find(group => group.uid === Number(remove.dataset.removeOverride)); if (group) delete group.values[remove.dataset.key]; renderOverrides(); markDirty(); if (group) $(`#group-id-${group.uid}`).focus(); return; }
    const add = event.target.closest('[data-add-override]');
    if (add) { const group = S.groups.find(group => group.uid === Number(add.dataset.addOverride)); const key = group && GROUP_KEYS.find(key => !(key in group.values)); if (key) { group.values[key] = S.settings[key] ?? DEFAULTS[key]; renderOverrides(); markDirty(); $(`[data-override-key="${group.uid}"][data-old-key="${key}"]`)?.focus(); } }
  });
  $('#add-group').addEventListener('click',() => { const group = {uid:++S.groupSeq,id:'',values:{}}; S.groups.push(group); renderOverrides(); markDirty(); $(`#group-id-${group.uid}`).focus(); });
  $('#settings-form').addEventListener('submit',saveSettings);
  $('#settings-fields').addEventListener('change',event => {
    if (event.target.id !== 'font-file') return;
    S.fontFile = event.target.files?.[0] || null; S.fontError = ''; S.fontStatus = '';
    if (S.fontFile) {
      try { validateFontFile(S.fontFile); }
      catch (error) { S.fontError = error.message; S.fontFile = null; }
    }
    updateFontControls();
  });
  $('#settings-fields').addEventListener('click',event => {
    if (event.target.closest('#renderer-refresh')) refreshRenderer();
    const prepare = event.target.closest('[data-renderer-prepare]');
    if (prepare) prepareRenderer(prepare.dataset.rendererPrepare);
    if (event.target.closest('#font-choose-action')) $('#font-file').click();
    if (event.target.closest('#font-upload-action')) uploadFont();
    if (event.target.closest('#font-reset-action')) resetFont();
  });
  $('#settings-reset').addEventListener('click',() => { populateSettings(); toast('已恢复后端保存的设置。'); });
  $('#reconnect-button').addEventListener('click',connect);
  window.addEventListener('beforeunload',event => { if (S.dirty) { event.preventDefault(); event.returnValue = ''; } });
  window.addEventListener('pagehide',() => { clearTimeout(S.pollTimer); clearTimeout(S.jobTimer); });
  document.addEventListener('visibilitychange',() => { if (!document.hidden && S.bridge) refreshState({quiet:true}); });
}
async function pollState() {
  if (!document.hidden && S.bridge) await refreshState({quiet:true});
  S.pollTimer = setTimeout(pollState,S.online ? 5500 : 12000);
}
async function pollJob() {
  const id = S.currentId, job = S.jobs.get(id);
  if (!document.hidden && S.bridge && job && ACTIVE.has(job.status) && !S.jobBusy) {
    S.jobBusy = true;
    try {
      const data = await apiGet('job',{id});
      if (data?.job) { rememberJob(data.job); if (S.currentId === id) updateCurrentJob(); if (!ACTIVE.has(data.job.status)) refreshState({quiet:true}); }
    } catch { /* The state poll exposes connection failures without toast spam. */ }
    finally { S.jobBusy = false; }
  }
  S.jobTimer = setTimeout(pollJob,1600);
}
async function connect() {
  connection('connecting');
  const bridge = window.AstrBotPluginView || window.AstrBotPluginPage;
  if (!bridge || typeof bridge.ready !== 'function' || typeof bridge.apiGet !== 'function' || typeof bridge.apiPost !== 'function') { connection('offline'); return; }
  try {
    await Promise.race([bridge.ready(),new Promise((_,reject) => setTimeout(() => reject(new Error('等待 AstrBot 页面桥接超时')),12000))]);
    S.bridge = bridge;
    await refreshState({quiet:true});
  } catch (error) { connection('offline',`${error.message}。请从 AstrBot 插件管理打开此页面，并确认插件已启用。`); }
}
function init() {
  try {
    const saved = localStorage.getItem('tieba-lens-color-mode');
    const dark = saved ? saved === 'dark' : matchMedia('(prefers-color-scheme:dark)').matches;
    document.documentElement.dataset.colorMode = dark ? 'dark' : 'light';
    $('#color-mode').setAttribute('aria-label',dark ? '切换浅色模式' : '切换深色模式');
  } catch {}
  const now = new Date();
  $('#today-day').textContent = String(now.getDate()).padStart(2,'0');
  const dateParts = document.createElement('span');
  dateParts.textContent = now.toLocaleDateString('zh-CN',{year:'numeric',month:'long'});
  const weekday = document.createElement('span'); weekday.textContent = now.toLocaleDateString('zh-CN',{weekday:'long'});
  $('#today-date').replaceChildren(dateParts,document.createElement('br'),weekday);
  setupThemeControls();
  populateSettings();
  bindEvents();
  navigate(location.hash.slice(1));
  connection('connecting');
  renderHistory();
  connect();
  S.pollTimer = setTimeout(pollState,5500);
  S.jobTimer = setTimeout(pollJob,1600);
}
init();
