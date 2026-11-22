import{_ as h,q as $,o,c as a,b as t,F as m,r as S,t as d,p as C,E as c,m as y,g as p}from"./index-DpjsNNUc.js";const L={class:"ctrl panel"},x=["disabled"],g={class:"panel"},I={class:"panel__body"},T={key:0,class:"mono-sm",style:{color:"var(--text-mute)"}},E={key:1,class:"kv"},B={class:"kv__k"},K={class:"kv__v"},w=["title","onClick"],M={key:1,class:"kv__v kv__v--dash"},V={class:"panel"},P={class:"panel__hd"},A=["disabled"],D={class:"panel__body"},F={class:"code"},N={class:"panel"},R={class:"panel__hd"},j=["disabled"],q={class:"panel__body"},U={class:"code"},z={__name:"SkillView",setup(G){const i=y(null),r=y(!1);async function k(){r.value=!0;try{i.value=await C.skill()}catch(s){c.error(s.message)}finally{r.value=!1}}$(k);async function u(s,e){const l=String(s||"");if(l){try{await navigator.clipboard.writeText(l),c.success(`已复制${e}`);return}catch{}try{const n=document.createElement("textarea");n.value=l,n.style.position="fixed",n.style.opacity="0",document.body.appendChild(n),n.select();const f=document.execCommand("copy");if(document.body.removeChild(n),f){c.success(`已复制${e}`);return}}catch{}c.warning("复制失败，请手动选中文本复制")}}const b=p(()=>{const s=i.value;return s?[{k:"Skill 名称",v:s.name,copy:!1},{k:"Skill 目录（绝对路径）",v:s.skill_dir,copy:!0},{k:"SKILL.md（绝对路径）",v:s.skill_md,copy:!0},{k:"CLI 脚本（绝对路径）",v:s.script,copy:!0},{k:"参考文档",v:(s.references||[]).join("、")||"—",copy:!1},{k:"项目根目录",v:s.root,copy:!0},{k:"Python 解释器",v:s.python,copy:!0},{k:"本机管家端口",v:String(s.hub_port||5190),copy:!1},{k:"安装状态",v:s.installed_path?`已安装：${s.installed_path}`:"未检测到安装目录",copy:!1}]:[]}),_=p(()=>{const s=i.value;return s?`请安装本项目的 TTS 配音 Skill（name: ${s.name}）。

Skill 目录（绝对路径）：${s.skill_dir}
SKILL.md（绝对路径）：${s.skill_md}
CLI 脚本（绝对路径）：${s.script}
参考文档目录：${s.skill_dir}\\references
Python 解释器：${s.python}
本机管家端口：${s.hub_port}

安装步骤：
1. 把整个 Skill 目录（${s.skill_dir}）复制或以软链接方式装进智能体的技能目录，目录名必须是 ${s.name}：
   - CodeBuddy：C:\\Users\\<用户名>\\.codebuddy\\skills\\${s.name}
   - Claude Code：<项目目录>\\.claude\\skills\\${s.name}
   - 其他智能体：放到它约定的 skills 目录即可
2. 不要改动 SKILL.md 的 frontmatter（name / description），那是智能体识别并触发该技能的依据；
3. 安装后读一遍 SKILL.md 与 references\\interfaces.md，确认 scripts\\tts_agent.py 可以执行；
4. 不要修改 skill 内的脚本与文档（它们与本机端口、引擎配置绑定），需要调整时先问我。

安装完成后，请用一句话确认已安装，并告诉我如何用它配音。`:""}),v=p(()=>{const s=i.value;return s?`安装完成后，直接对智能体说下面任意一句就会触发配音（它会自动调用 ${s.name} Skill → 请求本机 ${s.hub_port} 管家 → 合成 wav）：

· 把这段文字转成语音：……
· 用 VoxCPM 克隆这个音色，配音：……
· 用上次那个接口配音：……
· 把 README.md 读成语音

智能体实际执行的命令（供参考，由它自己决定）：
"${s.python}" "${s.script}" run --interface last --text "要合成的文本"

常用子命令：interfaces（列出可用接口）/ last（上次用的接口与参考音频）/ run（合成并落盘）/ status（管家与引擎进程状态）/ stop（清理进程、释放显存）。`:""});return(s,e)=>(o(),a("div",null,[t("div",L,[e[2]||(e[2]=t("span",{class:"hud"},"SKILL INSTALL",-1)),e[3]||(e[3]=t("span",{class:"mono-sm"}," 让智能体学会调用本机 TTS 管家配音 —— 复制下面的提示词发给它即可完成安装 ",-1)),e[4]||(e[4]=t("div",{class:"spacer"},null,-1)),t("button",{class:"btn",disabled:r.value,onClick:k},"刷新",8,x)]),t("div",g,[e[5]||(e[5]=t("div",{class:"panel__hd"},[t("div",{class:"panel__title"},"Skill 本体 · 绝对路径"),t("span",{class:"mono-sm",style:{color:"var(--text-mute)"}}," 路径由后端实时给出，可直接复制给智能体 ")],-1)),t("div",I,[i.value?(o(),a("div",E,[(o(!0),a(m,null,S(b.value,l=>(o(),a(m,{key:l.k},[t("div",B,d(l.k),1),t("code",K,d(l.v),1),l.copy?(o(),a("button",{key:0,class:"btn btn--ghost btn--xs",title:`复制 ${l.v}`,onClick:n=>u(l.v,l.k)}," 复制 ",8,w)):(o(),a("div",M,"—"))],64))),128))])):(o(),a("div",T,"加载中…"))])]),t("div",V,[t("div",P,[e[6]||(e[6]=t("div",{class:"panel__title"},"① 安装提示词 · 整段复制给智能体",-1)),t("button",{class:"btn btn--primary",disabled:!_.value,onClick:e[0]||(e[0]=l=>u(_.value,"安装提示词"))}," 一键复制提示词 ",8,A)]),t("div",D,[t("pre",F,d(_.value),1),e[7]||(e[7]=t("div",{class:"note"}," 提醒：请把上面的提示词完整粘贴给你的智能体（CodeBuddy / Claude Code / Cursor 等）， 让它按提示把 Skill 装进自己的技能目录；安装后智能体就能直接调用本机 5190 管家配音。 ",-1))])]),t("div",N,[t("div",R,[e[8]||(e[8]=t("div",{class:"panel__title"},"② 装好后如何唤醒智能体配音",-1)),t("button",{class:"btn btn--ghost",disabled:!v.value,onClick:e[1]||(e[1]=l=>u(v.value,"唤醒示例"))}," 复制唤醒示例 ",8,j)]),t("div",q,[t("pre",U,d(v.value),1),e[9]||(e[9]=t("div",{class:"note"}," 智能体被唤醒后会自动：确认接口 → 拉起管家/引擎 → 合成 wav → 关闭进程释放显存， 并把生成的音频绝对路径报给你。全程无需手工敲命令。 ",-1))])])]))}},J=h(z,[["__scopeId","data-v-6a32b626"]]);export{J as default};
