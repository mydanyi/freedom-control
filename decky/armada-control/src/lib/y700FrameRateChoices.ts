import { afterPatch, findModuleExport, modules } from "@decky/ui";
import { Y700_DEVICE_ID, filterY700FrameRates } from "./frameRatePolicy";

// Steam reports Gamescope state through this native query. The plugin only
// reuses the existing cache entry so Steam's own notification keeps driving
// future updates; it never registers its own subscription.
const QUERY_CLIENT_MODULE_ID = "40208";
const QUERY_KEY = ["GamescopeService", "State"];

export function installY700FrameRateChoices(deviceId: string): () => void {
  const noop = () => {};
  if (deviceId !== Y700_DEVICE_ID) return noop;

  const owner: any = findModuleExport(
    (value: any) =>
      value?.GetStateHandler?.name === "Gamescope.GetState#1"
      && typeof value.GetState === "function",
  );
  if (!owner) {
    console.warn("[armada-control] Y700 frame-rate filter disabled: Gamescope.GetState owner not found");
    return noop;
  }

  const client: any = modules.get(QUERY_CLIENT_MODULE_ID)?.L;
  if (
    !client
    || typeof client.getQueryCache !== "function"
    || typeof client.cancelQueries !== "function"
    || typeof client.invalidateQueries !== "function"
  ) {
    console.warn("[armada-control] Y700 frame-rate filter disabled: GamescopeService.State query client unavailable");
    return noop;
  }

  let active = true;

  // The reply is only cloned on the way out; native replies, their prototypes,
  // and the native query cache are never mutated.
  const transformSerialized = (serialized: any): any => {
    if (!active) return serialized;
    if (!serialized || typeof serialized !== "object") return serialized;
    const nextState = filterY700FrameRates(deviceId, serialized.state);
    if (nextState === serialized.state) return serialized;
    return { ...serialized, state: nextState };
  };

  const wrapBody = (body: any): any => {
    if (!body || typeof body !== "object") return body;
    const toObject = Reflect.get(body, "toObject", body);
    if (typeof toObject !== "function") return body;
    return new Proxy(body, {
      get(target, key) {
        if (key === "toObject") {
          return (...args: any[]) => transformSerialized(toObject.apply(target, args));
        }
        const value = Reflect.get(target, key, target);
        return typeof value === "function" ? value.bind(target) : value;
      },
    });
  };

  const wrapReply = (reply: any): any =>
    new Proxy(reply, {
      get(target, key) {
        if (key === "Body") {
          return (...args: any[]) => wrapBody(Reflect.get(target, "Body", target).apply(target, args));
        }
        const value = Reflect.get(target, key, target);
        return typeof value === "function" ? value.bind(target) : value;
      },
    });

  // A rejected native GetState promise passes straight through; the patch adds
  // no catch and no retry loop.
  const patch: any = afterPatch(owner, "GetState", (_args: any[], ret: any) => {
    if (!ret || typeof ret.then !== "function") return ret;
    return ret.then((reply: any) => {
      if (!active) return reply;
      if (!reply || typeof reply.BSuccess !== "function" || typeof reply.Body !== "function") return reply;
      if (!reply.BSuccess()) return reply;
      return wrapReply(reply);
    });
  });

  const refresh = async (mode: "install" | "dispose" = "install") => {
    try {
      await client.cancelQueries({ queryKey: QUERY_KEY, exact: true });
      if (mode === "install" && !active) return;
      await client.invalidateQueries({ queryKey: QUERY_KEY, exact: true, refetchType: "all" });
    } catch (error) {
      console.warn("[armada-control] Y700 frame-rate query refresh failed", error);
    }
  };

  void refresh();

  return () => {
    if (!active) return;
    active = false;
    try {
      patch?.unpatch?.();
    } catch (error) {
      console.warn("[armada-control] Y700 frame-rate unpatch failed", error);
    }
    void refresh("dispose");
  };
}
