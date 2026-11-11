import icons from "../vendor/harness/icons.json";

export type IconName = keyof typeof icons;

export function Icon({ name, size = 18 }: { name: IconName; size?: number }) {
  const icon = icons[name];
  // The SVG markup is bundled Harness UI artwork. No runtime data enters it.
  return <svg width={size} height={size} viewBox={icon.viewBox} fill={icon.filled ? "currentColor" : "none"} stroke={icon.filled ? "none" : "currentColor"} strokeWidth={icon.strokeWidth} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" dangerouslySetInnerHTML={{ __html: icon.body }} />;
}
