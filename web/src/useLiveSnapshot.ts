import { useCallback, useEffect, useRef, useState } from "react";
import { fetchSnapshot, type Snapshot } from "./lib";

export type SyncState = "connecting" | "live" | "reconnecting" | "offline";

export function useLiveSnapshot() {
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [error, setError] = useState(false);
  const [sync, setSync] = useState<SyncState>("connecting");
  const requestId = useRef(0);
  const mounted = useRef(true);
  const hasSnapshot = useRef(false);

  const load = useCallback(async () => {
    const request = ++requestId.current;
    try {
      const next = await fetchSnapshot();
      if (!mounted.current || request !== requestId.current) return;
      hasSnapshot.current = true;
      setSnapshot(next);
      setError(false);
    } catch {
      if (mounted.current && request === requestId.current) setError(true);
    }
  }, []);

  useEffect(() => {
    mounted.current = true;
    let closed = false;
    let retry: number | undefined;
    let attempts = 0;
    let socket: WebSocket | null = null;

    const connect = () => {
      if (closed) return;
      setSync(hasSnapshot.current ? "reconnecting" : "connecting");
      socket = new WebSocket(
        `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/api/ws`
      );
      socket.onopen = () => {
        if (closed) return;
        attempts = 0;
        setSync("live");
        void load();
      };
      socket.onmessage = (event) => {
        try {
          const message = JSON.parse(event.data) as { type?: string };
          if (message.type === "ready" || message.type === "refresh") void load();
        } catch {
          // A malformed refresh signal must not overwrite the REST snapshot.
        }
      };
      socket.onerror = () => socket?.close();
      socket.onclose = () => {
        if (closed) return;
        setSync("reconnecting");
        retry = window.setTimeout(connect, Math.min(8000, 500 * 2 ** attempts));
        attempts += 1;
      };
    };

    void load();
    connect();
    return () => {
      closed = true;
      mounted.current = false;
      if (retry !== undefined) window.clearTimeout(retry);
      socket?.close();
    };
  }, [load]);

  return { snapshot, error, sync, reload: load };
}
