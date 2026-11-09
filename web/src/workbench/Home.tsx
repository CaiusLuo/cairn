import { href } from "./Shell";
import { CairnMark, Icon, type IconName } from "./Icon";

export const shortcuts: { href: string; title: string; detail: string; icon: IconName }[] = [
  { href: href("tasks"), title: "开发任务", detail: "查看共享任务、工作树与执行结果。", icon: "board" },
  { href: href("runs"), title: "运行记录", detail: "查看来自 Web、CLI 和微信的运行。", icon: "runs" },
  { href: href("settings"), title: "工作台设置", detail: "管理微信连接、定时任务与个人记忆。", icon: "settings" },
];

export function BrandIntro() {
  return <div className="wb-intro"><CairnMark size={64}/><div><h1>Cairn</h1><p>从一个问题开始。</p></div></div>;
}
