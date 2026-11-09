import icons from "../vendor/harness/icons.json";

export type IconName = keyof typeof icons;

export function Icon({ name, size = 18 }: { name: IconName; size?: number }) {
  const icon = icons[name];
  // The SVG markup is bundled Harness UI artwork. No runtime data enters it.
  return <svg width={size} height={size} viewBox={icon.viewBox} fill={icon.filled ? "currentColor" : "none"} stroke={icon.filled ? "none" : "currentColor"} strokeWidth={icon.strokeWidth} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" dangerouslySetInnerHTML={{ __html: icon.body }} />;
}

export function CairnMark({ size = 28 }: { size?: number }) {
  return <svg width={size} height={size} viewBox="0 0 64 64" fill="none" aria-hidden="true">
    <path d="M9 50c3-5 12-9 23-9s20 4 23 9c2 4-3 8-10 9H19C12 58 7 54 9 50Z" fill="#7862ed"/>
    <path d="M16 34c3-5 9-8 16-8s14 3 16 8c2 4-3 7-8 8H24c-5-1-10-4-8-8Z" fill="#9b88ff"/>
    <path d="M23 20c1-5 5-10 10-10 4 0 8 5 9 10 0 4-4 6-9 6s-10-2-10-6Z" fill="#baabff"/>
  </svg>;
}
