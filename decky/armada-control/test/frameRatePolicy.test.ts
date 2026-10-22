import assert from "node:assert/strict";
import test from "node:test";

import { filterY700FrameRates } from "../src/lib/frameRatePolicy.ts";

const DEVICE = "lenovo-tb323fu";

// The internal panel advertises five refresh rates, while Steam's native FPS
// list additionally offers 10/12/.../82 for the same display.
function internalState(display: Record<string, unknown> = {}) {
  return {
    active_display_info: {
      connector_name: "DSI-1",
      is_external: false,
      supported_refresh_rates: [30, 60, 90, 120, 164],
      supported_frame_rates: [10, 12, 20, 24, 30, 40, 50, 60, 82, 90, 120, 164],
      ...display,
    },
    hdr_enabled: true,
  };
}

test("narrows the internal Y700 panel to exactly the five chosen rates", () => {
  const result = filterY700FrameRates(DEVICE, internalState());
  assert.deepEqual(result.active_display_info.supported_frame_rates, [30, 60, 90, 120, 164]);
});

test("drops the native extra rates but leaves the refresh list alone", () => {
  const input = internalState();
  const refresh = input.active_display_info.supported_refresh_rates;
  const result = filterY700FrameRates(DEVICE, input);
  assert.strictEqual(result.active_display_info.supported_refresh_rates, refresh);
  for (const extra of [10, 12, 20, 24, 40, 50, 82]) {
    assert.equal(result.active_display_info.supported_frame_rates.includes(extra), false);
  }
});

test("surviving rates keep the picker order regardless of input order", () => {
  const unordered = internalState({
    supported_refresh_rates: [164, 30, 120, 60, 90],
    supported_frame_rates: [164, 90, 60, 30, 120],
  });
  assert.deepEqual(filterY700FrameRates(DEVICE, unordered).active_display_info.supported_frame_rates, [30, 60, 90, 120, 164]);
});

test("rates the panel never advertised are not invented", () => {
  const limited = internalState({
    supported_refresh_rates: [60, 120],
    supported_frame_rates: [30, 60, 90, 120, 164],
  });
  assert.deepEqual(filterY700FrameRates(DEVICE, limited).active_display_info.supported_frame_rates, [60, 120]);
});

test("an FPS-only rate missing from the advertised refresh list is removed", () => {
  const fpsOnly = internalState({
    supported_refresh_rates: [30, 60],
    supported_frame_rates: [30, 60, 90],
  });
  assert.deepEqual(filterY700FrameRates(DEVICE, fpsOnly).active_display_info.supported_frame_rates, [30, 60]);
});

test("a non-Y700 or inexact device id returns the same object", () => {
  const input = internalState();
  assert.strictEqual(filterY700FrameRates("lenovo-tb323fu-other", input), input);
  assert.strictEqual(filterY700FrameRates("LENOVO-TB323FU", input), input);
  assert.strictEqual(filterY700FrameRates("", input), input);
});

test("an external display or a non-DSI connector is returned unchanged", () => {
  const external = internalState({ is_external: true });
  assert.strictEqual(filterY700FrameRates(DEVICE, external), external);
  const unknownExternal = internalState({ is_external: undefined });
  assert.strictEqual(filterY700FrameRates(DEVICE, unknownExternal), unknownExternal);
  const otherConnector = internalState({ connector_name: "DP-1" });
  assert.strictEqual(filterY700FrameRates(DEVICE, otherConnector), otherConnector);
});

test("missing or empty rate arrays produce no change", () => {
  const noFps = internalState({ supported_frame_rates: undefined });
  assert.strictEqual(filterY700FrameRates(DEVICE, noFps), noFps);
  const noRefresh = internalState({ supported_refresh_rates: undefined });
  assert.strictEqual(filterY700FrameRates(DEVICE, noRefresh), noRefresh);
  const emptyFps = internalState({ supported_frame_rates: [] });
  assert.strictEqual(filterY700FrameRates(DEVICE, emptyFps), emptyFps);
  const noDisplay = { hdr_enabled: false };
  assert.strictEqual(filterY700FrameRates(DEVICE, noDisplay), noDisplay);
});

test("the input object and its arrays are never mutated", () => {
  const input = internalState();
  const refresh = input.active_display_info.supported_refresh_rates;
  const fps = input.active_display_info.supported_frame_rates;
  const marker = input.hdr_enabled;
  const result = filterY700FrameRates(DEVICE, input);
  assert.notStrictEqual(result, input);
  assert.notStrictEqual(result.active_display_info, input.active_display_info);
  assert.strictEqual(result.hdr_enabled, marker);
  assert.deepEqual(input.active_display_info.supported_frame_rates, [10, 12, 20, 24, 30, 40, 50, 60, 82, 90, 120, 164]);
  assert.strictEqual(input.active_display_info.supported_refresh_rates, refresh);
  assert.strictEqual(input.active_display_info.supported_frame_rates, fps);
});

// --- 165 Hz native-mode candidate behavior (test-first; implementation not
// updated yet). A tier must appear in BOTH advertised lists. New 165 kernels
// yield five tiers ending in 165; old 164 kernels still yield five tiers
// ending in 164, and 165 is never invented from the FPS list alone.

test("new 165 native kernel narrows the picker to five tiers ending in 165", () => {
  const input = internalState({
    supported_refresh_rates: [30, 60, 90, 120, 165],
    supported_frame_rates: [10, 12, 20, 24, 30, 40, 50, 60, 82, 90, 120, 165],
  });
  assert.deepEqual(filterY700FrameRates(DEVICE, input).active_display_info.supported_frame_rates, [30, 60, 90, 120, 165]);
});

test("old 164 native kernel keeps five tiers ending in 164 for rollback", () => {
  const input = internalState({
    supported_refresh_rates: [164, 30, 120, 60, 90],
    supported_frame_rates: [164, 90, 60, 30, 120],
  });
  assert.deepEqual(filterY700FrameRates(DEVICE, input).active_display_info.supported_frame_rates, [30, 60, 90, 120, 164]);
});

test("a 165 declared only by the FPS list is not invented over the 164 panel", () => {
  const input = internalState({
    supported_refresh_rates: [30, 60, 90, 120, 164],
    supported_frame_rates: [10, 12, 20, 24, 30, 40, 50, 60, 82, 90, 120, 164, 165],
  });
  assert.deepEqual(filterY700FrameRates(DEVICE, input).active_display_info.supported_frame_rates, [30, 60, 90, 120, 164]);
});

test("both 164 and 165 advertised in both lists choose 165 and exclude 164", () => {
  const input = internalState({
    supported_refresh_rates: [30, 60, 90, 120, 164, 165],
    supported_frame_rates: [10, 12, 20, 24, 30, 40, 50, 60, 82, 90, 120, 164, 165],
  });
  const refresh = input.active_display_info.supported_refresh_rates;
  const result = filterY700FrameRates(DEVICE, input);
  assert.deepEqual(result.active_display_info.supported_frame_rates, [30, 60, 90, 120, 165]);
  assert.equal(result.active_display_info.supported_frame_rates.includes(164), false);
  assert.strictEqual(result.active_display_info.supported_refresh_rates, refresh);
});

test("a 165-ready fixture is returned unchanged on inexact device and external panel", () => {
  const inexact = internalState({
    supported_refresh_rates: [30, 60, 90, 120, 165],
    supported_frame_rates: [10, 12, 20, 24, 30, 40, 50, 60, 82, 90, 120, 165],
  });
  assert.strictEqual(filterY700FrameRates("lenovo-tb323fu-other", inexact), inexact);

  const external = internalState({
    is_external: true,
    supported_refresh_rates: [30, 60, 90, 120, 165],
    supported_frame_rates: [10, 12, 20, 24, 30, 40, 50, 60, 82, 90, 120, 165],
  });
  assert.strictEqual(filterY700FrameRates(DEVICE, external), external);

  const otherConnector = internalState({
    connector_name: "DP-1",
    supported_refresh_rates: [30, 60, 90, 120, 165],
    supported_frame_rates: [10, 12, 20, 24, 30, 40, 50, 60, 82, 90, 120, 165],
  });
  assert.strictEqual(filterY700FrameRates(DEVICE, otherConnector), otherConnector);
});
