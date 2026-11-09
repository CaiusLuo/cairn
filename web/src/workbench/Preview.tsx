import { Shell, href, type View } from "./Shell";
import { CairnMark, Icon, type IconName } from "./Icon";
import { useResource, type Conversation } from "./data";

export const shortcuts: { href: string; title: string; detail: string; icon: IconName }[] = [
  { href: href("tasks"), title: "开发任务", detail: "查看共享任务、工作树与执行结果。", icon: "board" },
  { href: href("runs"), title: "运行记录", detail: "查看来自 Web、CLI 和微信的运行。", icon: "runs" },
  { href: href("settings"), title: "工作台设置", detail: "管理微信连接、定时任务与个人记忆。", icon: "settings" },
];

export function BrandIntro() {
  return <div className="wb-intro"><CairnMark size={64}/><div><h1>Cairn</h1><p>从一个问题开始。</p></div></div>;
}

export function Preview() {
  const workspace = useResource<{ workspace: string }>("workspace", 4000);
  const conversations = useResource<Conversation[]>("conversations", 2500);
  const requested = new URLSearchParams(location.search).get("view");
  const view: View = requested === "runs" || requested === "workflow" || requested === "approvals" ? requested : "home";
  return <Shell view={view} conversations={conversations.data ?? []} workspace={workspace.data?.workspace} status={workspace.error ? "offline" : workspace.data ? "online" : "connecting"} navigate={url => location.assign(url)}>
    <div className="wb-home"><div className="wb-home-inner"><BrandIntro/>
      {view !== "home" && <p role="status">此视图将在 Phase B 接入共享 Server 数据。</p>}
      <a className="wb-start" href="/chat.html"><Icon name="edit"/>进入对话工作台<Icon name="arrow-right"/></a>
      <div className="wb-shortcuts">{shortcuts.map(item => <a href={item.href === href("runs") ? "/index.html?view=runs" : item.href} key={item.title}><Icon name={item.icon}/><strong>{item.title}</strong><small>{item.detail}</small></a>)}</div>
    </div></div>
  </Shell>;
}
