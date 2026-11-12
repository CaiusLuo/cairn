import { useEffect, useRef } from "react";
import cairnMark from "../assets/cairn-mark.svg";
import "./boot.css";

type BootScreenProps = { dark?: boolean; label?: string; surface?: "login" };

export function BootScreen({ dark = false, label = "正在加载 Cairn…", surface }: BootScreenProps) {
  const mark = useRef<HTMLImageElement>(null);
  const shadow = useRef<HTMLDivElement>(null);
  const labelRef = useRef<HTMLSpanElement>(null);

  useEffect(() => {
    let canceled = false;
    let cleanup: (() => void) | undefined;
    void import("gsap")
      .then(({ gsap }) => {
        const markElement = mark.current;
        const shadowElement = shadow.current;
        const labelElement = labelRef.current;
        if (canceled || !markElement || !shadowElement || !labelElement) return;
        const letters = labelElement.querySelectorAll<HTMLElement>(".boot-letter");
        const peakColor = getComputedStyle(labelElement).getPropertyValue("--boot-letter-peak").trim();
        const media = gsap.matchMedia();
        media.add("(prefers-reduced-motion: no-preference)", () => {
          gsap.set(markElement, { transformOrigin: "50% 100%" });
          gsap.from(labelElement, { autoAlpha: 0, duration: 0.45, delay: 0.1, ease: "power2.out" });
          const jump = gsap.timeline({ repeat: -1, repeatDelay: 0.14 });
          jump
            .to(markElement, { y: -34, duration: 0.36, ease: "power2.out" }, 0)
            .to(shadowElement, { scaleX: 0.55, opacity: 0.12, duration: 0.36, ease: "power2.out" }, 0)
            .to(markElement, { y: 0, duration: 0.3, ease: "power2.in" }, 0.36)
            .to(shadowElement, { scaleX: 1, opacity: 0.3, duration: 0.3, ease: "power2.in" }, 0.36)
            .to(markElement, { scaleX: 1.16, scaleY: 0.84, duration: 0.08, ease: "power2.out" }, 0.62)
            .to(markElement, { scaleX: 1, scaleY: 1, duration: 0.5, ease: "elastic.out(1, 0.35)" }, 0.7);
          // Letter hops are transform-only and stay inside the reserved line box,
          // so the label's baseline never moves.
          const wave = gsap.to(letters, {
            y: -4,
            color: peakColor || undefined,
            duration: 0.36,
            ease: "sine.inOut",
            stagger: { each: 0.05 },
            repeat: -1,
            yoyo: true,
            repeatDelay: 0.28,
            delay: 0.45,
          });
          return () => {
            jump.kill();
            wave.kill();
          };
        });
        cleanup = () => media.revert();
      })
      .catch(() => undefined);
    return () => {
      canceled = true;
      cleanup?.();
    };
  }, []);

  return (
    <div className="boot-screen" data-dark={dark} data-surface={surface} role="status" aria-live="polite" aria-label={label}>
      <div className="boot-stage">
        <img ref={mark} className="boot-mark" src={cairnMark} alt="" aria-hidden="true" />
        <div ref={shadow} className="boot-shadow" aria-hidden="true" />
      </div>
      <span ref={labelRef} className="boot-label" aria-hidden="true">
        {Array.from(label).map((char, index) => (
          <span key={index} className="boot-letter">
            {char === " " ? " " : char}
          </span>
        ))}
      </span>
    </div>
  );
}
