import { href } from "./Shell";
import { CairnLogo } from "./Brand";
import { Icon, type IconName } from "./Icon";

export const shortcuts: { href: string; title: string; icon: IconName }[] = [
  { href: href("tasks"), title: "开发任务", icon: "board" },
  { href: href("runs"), title: "运行记录", icon: "runs" },
  { href: href("settings"), title: "工作台设置", icon: "settings" },
];

export function BrandIntro() {
  return <div className="wb-intro"><h1 className="wb-intro-heading"><CairnLogo className="wb-intro-logo"/></h1></div>;
}
