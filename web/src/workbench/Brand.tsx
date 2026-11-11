import cairnMark from "../assets/cairn-mark.svg";
import cairnWordmark from "../assets/cairn-wordmark.svg";
import cairnWordmarkDark from "../assets/cairn-wordmark-dark.svg";

export function CairnLogo({ className = "" }: { className?: string }) {
  return <span className={`cairn-logo${className ? ` ${className}` : ""}`}>
    <img className="cairn-logo__light" src={cairnWordmark} alt="Cairn"/>
    <img className="cairn-logo__dark" src={cairnWordmarkDark} alt="" aria-hidden="true"/>
  </span>;
}

export function CairnMark({ size = 28 }: { size?: number }) {
  return <img className="cairn-mark" src={cairnMark} width={size} height={size} alt="" aria-hidden="true"/>;
}
