import type { SyncState } from "../useLiveSnapshot";

const LABELS: Record<SyncState, string> = {
  connecting: "Connecting",
  live: "Live",
  reconnecting: "Reconnecting",
  offline: "Offline"
};

export function SyncBadge({ sync }: { sync: SyncState }) {
  return (
    <span className={`sync-badge sync-badge--${sync}`} title={`Snapshot ${LABELS[sync]}`}>
      <span className="sync-badge__dot" />
      {LABELS[sync]}
    </span>
  );
}
