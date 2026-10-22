// Steam derives additional divisor frame-rate items from a display's refresh
// rate, so its native picker can list more FPS entries than this device's owner
// wants. Keep only five tiers — the four base rates below plus one native high
// tier — and delete the rest; nothing else is touched.

export const Y700_DEVICE_ID = "lenovo-tb323fu";
export const Y700_INTERNAL_CONNECTOR = "DSI-1";

// Ordered by preference; the output keeps this order, not the input's.
const Y700_BASE_FPS_CHOICES = [30, 60, 90, 120];

interface DisplayInfo {
  connector_name?: string;
  is_external?: boolean;
  supported_refresh_rates?: number[];
  supported_frame_rates?: number[];
  [key: string]: unknown;
}

interface GamescopeState {
  active_display_info?: DisplayInfo;
  [key: string]: unknown;
}

// A non-Y700 device, an external panel, or a connector other than the internal
// DSI-1 returns the input object unchanged. Only rates advertised by both the
// refresh list and the frame-rate list survive, so an unavailable rate is never
// fabricated into the picker.
export function filterY700FrameRates(deviceId: string, state: GamescopeState): GamescopeState {
  if (deviceId !== Y700_DEVICE_ID || !state || typeof state !== "object") return state;
  const display = state.active_display_info;
  if (!display || typeof display !== "object") return state;
  if (display.is_external !== false || display.connector_name !== Y700_INTERNAL_CONNECTOR) return state;
  const advertisedRefresh = display.supported_refresh_rates;
  const advertisedFps = display.supported_frame_rates;
  if (!Array.isArray(advertisedRefresh) || !Array.isArray(advertisedFps)) return state;
  const refresh = new Set(advertisedRefresh);
  const fps = new Set(advertisedFps);
  const highTier = refresh.has(165) && fps.has(165) ? 165 : 164;
  const choices = [...Y700_BASE_FPS_CHOICES, highTier];
  const next = choices.filter((rate) => refresh.has(rate) && fps.has(rate));
  if (!next.length) return state;
  return {
    ...state,
    active_display_info: { ...display, supported_frame_rates: next },
  };
}
