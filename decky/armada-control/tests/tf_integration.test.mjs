// Controller <-> TF page integration checks. Both halves run real code: the TF
// backend module is loaded against a recording @decky/api stub (so the RPC
// names, argument order, envelope pass-through and rejected-call propagation
// are exercised), and the exported Content component is transpiled and rendered
// against a minimal React hook/jsx shim (so the tab list, the lazy TF mount and
// the locale mapping are read from the returned element tree, not from the
// source text).

import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { Buffer } from "node:buffer";
import ts from "typescript";

const compilerOptions = {
  module: ts.ModuleKind.ESNext,
  target: ts.ScriptTarget.ES2020,
  jsx: ts.JsxEmit.ReactJSX,
};

const transpile = (source, fileName) =>
  ts.transpileModule(source, { compilerOptions, fileName }).outputText;

const asDataUrl = (js) => `data:text/javascript;base64,${Buffer.from(js, "utf8").toString("base64")}`;

const toModuleUrl = (source, fileName = "module.ts") => asDataUrl(transpile(source, fileName));

const readSource = (relativePath) => readFile(new URL(relativePath, import.meta.url), "utf8");

// The real TF dictionary, used only as data: the tab label and the page locale
// expectations come from the same tables the page ships with.
const i18nUrl = toModuleUrl(await readSource("../src/tf/i18n.ts"), "i18n.ts");
const tfI18n = await import(i18nUrl);

// ---------------------------------------------------------------------------
// TF backend: executed RPC calls
// ---------------------------------------------------------------------------

// A recording @decky/api stand-in. Every call() is captured verbatim and its
// result is decided by __tfOutcome, so one stub covers a success envelope, an
// expected failure envelope and a rejected transport.
globalThis.__tfCalls = [];
const apiStubUrl = toModuleUrl(`
  export const call = (...args) => {
    globalThis.__tfCalls.push(args);
    return Promise.resolve(globalThis.__tfOutcome(...args));
  };
`);

const backendUrl = toModuleUrl(
  (await readSource("../src/tf/tf_backend.ts")).replace(
    /from ["']@decky\/api["']/,
    `from "${apiStubUrl}"`,
  ),
  "tf_backend.ts",
);
const backend = await import(backendUrl);

const okInspection = { ok: true, data: { cards: [{ device: "/dev/mmcblk0", state: "pending" }] } };
const failedPlan = { ok: false, error: "card_busy", detail: "the card is busy" };
const okStatus = { ok: true, data: { state: "running", phase: "preparing", detail: "", log_tail: "" } };

globalThis.__tfOutcome = (name) => (name === "prepare_tf_library" ? failedPlan : okInspection);
// A success envelope and an expected failure envelope both reach the caller
// unchanged; the wrapper neither unwraps data nor turns a failure into a throw.
assert.deepEqual(await backend.getTfInspection(), okInspection);
assert.deepEqual(await backend.prepareTfLibrary("/dev/mmcblk0"), failedPlan);

globalThis.__tfOutcome = () => okStatus;
assert.deepEqual(await backend.startTfLibrary("token-1", true), okStatus);
assert.deepEqual(await backend.startTfLibrary("token-2", false), okStatus);
assert.deepEqual(await backend.getTfStatus(), okStatus);

// RPC names and argument order: the card path comes from a prepared plan and
// the confirmation flag is forwarded verbatim, never coerced.
assert.deepEqual(globalThis.__tfCalls, [
  ["get_tf_inspection"],
  ["prepare_tf_library", "/dev/mmcblk0"],
  ["start_tf_library", "token-1", true],
  ["start_tf_library", "token-2", false],
  ["get_tf_status"],
]);

// A rejected transport call propagates to the page as the same rejection.
const transportClosed = new Error("RPC transport closed");
globalThis.__tfOutcome = () => Promise.reject(transportClosed);
await assert.rejects(backend.getTfStatus(), (error) => error === transportClosed);
await assert.rejects(backend.startTfLibrary("token-3", true), (error) => error === transportClosed);

// ---------------------------------------------------------------------------
// Content tab: executed render
// ---------------------------------------------------------------------------

// Small stateful hooks shim: values survive between renders, setters write back
// into the slot the caller indexes, refs are stable objects and no effect is
// ever scheduled. Only the slots Content reads through useState are seeded.
const reactStubUrl = toModuleUrl(`
  const store = () => globalThis.__tfHooks;
  export function useState(initial) {
    const state = store();
    const index = state.stateCursor++;
    if (index >= state.states.length) state.states.push(initial);
    const set = (next) => {
      const current = state.states[index];
      state.states[index] = typeof next === "function" ? next(current) : next;
    };
    return [state.states[index], set];
  }
  export function useRef(initial) {
    const state = store();
    const index = state.refCursor++;
    if (index >= state.refs.length) state.refs.push({ current: initial });
    return state.refs[index];
  }
  export function useCallback(fn) { return fn; }
  export function useMemo(fn) { return fn(); }
  export function useEffect() {}
`);

const contentStubs = {
  "@decky/ui": `export const Field = "Field";
export const PanelSection = "PanelSection";
export const Tabs = "Tabs";`,
  "react/jsx-runtime": `export const Fragment = "Fragment";
export const jsx = (type, props) => ({ type, props: props || {} });
export const jsxs = jsx;
export const jsxDEV = jsx;`,
  "./backend": `export const getConfig = async () => ({});
export const getInstalledGames = async () => [];
export const savePowerConfig = async () => {};
export const saveTweaks = async () => {};`,
  "./components/RgbLighting": `export const RgbLighting = "RgbLighting";`,
  "./hooks/useDebouncedSave": `export const useDebouncedSave = () => {};`,
  "./hooks/useLocale": `export const useLocale = () => globalThis.__tfLocale;`,
  "./i18n": `export const t = (key) => key;`,
  "./icons": `export const tabIcons = { Compatibility: {}, Power: {}, Fans: {}, RGB: {}, TF: {}, Advanced: {} };`,
  "./lib/games": `export const currentGame = () => null;`,
  "./styles": `export const styles = "";`,
  "./tabs/Compatibility": `export const Compatibility = "Compatibility";`,
  "./tabs/Fans": `export const Fans = "Fans";`,
  "./tabs/Power": `export const Power = "Power";`,
  "./tabs/Settings": `export const Settings = "Settings";`,
  "./tf/TfLibraryContent": `export const TfLibraryContent = "TfLibraryContent";`,
  // The page calls the real localiser so the rendered label is the real string.
  "./tf/tf_i18n": transpile(`import { translate } from "${i18nUrl}";
export const tfText = (locale, key, variables) => translate(locale, key, variables);
export const tfErrorText = (locale, code) => translate(locale, "error.unknown");`),
};

const stubUrls = {
  react: reactStubUrl,
  ...Object.fromEntries(
    Object.entries(contentStubs).map(([specifier, source]) => [specifier, asDataUrl(source)]),
  ),
};

const contentJs = transpile(await readSource("../src/Content.tsx"), "Content.tsx").replace(
  /from ["']([^"']+)["']/g,
  (_match, specifier) => {
    const url = stubUrls[specifier];
    if (!url) throw new Error(`Content.tsx imports an unstubbed module: ${specifier}`);
    return `from "${url}"`;
  },
);
const { Content } = await import(asDataUrl(contentJs));

// Element objects are { type, props }; a Tabs element carries its pages in the
// props.tabs list rather than in children, so both are followed.
const walk = (node, visit) => {
  if (Array.isArray(node)) {
    node.forEach((child) => walk(child, visit));
    return;
  }
  if (!node || typeof node !== "object") return;
  visit(node);
  const props = node.props || {};
  walk(props.children, visit);
  if (Array.isArray(props.tabs)) props.tabs.forEach((tab) => walk(tab.content, visit));
};

const findAll = (tree, predicate) => {
  const found = [];
  walk(tree, (node) => {
    if (predicate(node)) found.push(node);
  });
  return found;
};

const tabsOf = (tree) => findAll(tree, (node) => Array.isArray(node.props && node.props.tabs))[0];

const tfPages = (tree) => findAll(tree, (node) => node.type === "TfLibraryContent");

const tfTabOf = (tabsElement) => tabsElement.props.tabs.find((tab) => tab.id === "TF");

const tabIds = (tabsElement) => tabsElement.props.tabs.map((tab) => tab.id);

const baseConfig = { fanSupported: true, rgbSupported: true, installedGames: [], power: {}, tweaks: {} };

const mountContent = (locale, config = baseConfig) => {
  globalThis.__tfLocale = locale;
  globalThis.__tfHooks = {
    states: ["Compatibility", config, "Loading"],
    stateCursor: 0,
    refCursor: 0,
    refs: [],
  };
  return renderContent();
};

const renderContent = () => {
  const hooks = globalThis.__tfHooks;
  hooks.stateCursor = 0;
  hooks.refCursor = 0;
  return Content();
};

// The optional pages follow the device flags, and TF stays before Advanced.
const full = tabsOf(mountContent("en"));
assert.deepEqual(tabIds(full), ["Compatibility", "Power", "Fans", "RGB", "TF", "Advanced"]);
const bare = tabsOf(mountContent("en", { ...baseConfig, fanSupported: false, rgbSupported: false }));
assert.deepEqual(tabIds(bare), ["Compatibility", "Power", "TF", "Advanced"]);
const rgbOnly = tabsOf(mountContent("en", { ...baseConfig, fanSupported: false, rgbSupported: true }));
assert.deepEqual(tabIds(rgbOnly), ["Compatibility", "Power", "RGB", "TF", "Advanced"]);

// A fresh default mount: Compatibility is active and the TF page is absent, so
// no card probe or status poll can run before the tab is opened.
let tabsElement = tabsOf(mountContent("en"));
assert.ok(tabsElement, "Content renders a Tabs element once config is loaded");
assert.equal(tabsElement.props.activeTab, "Compatibility");
assert.equal(tfTabOf(tabsElement).content.props.children, null);
assert.equal(tfPages(tabsElement).length, 0);
assert.equal(tfTabOf(tabsElement).title.props["aria-label"], tfI18n.translate("en", "tf.title"));
assert.equal(tfTabOf(tabsElement).title.props.title, tfTabOf(tabsElement).title.props["aria-label"]);

// Activating the tab builds exactly one page with the mapped locale.
tabsElement.props.onShowTab("TF");
tabsElement = tabsOf(renderContent());
assert.equal(tabsElement.props.activeTab, "TF");
let pages = tfPages(tabsElement);
assert.equal(pages.length, 1);
assert.equal(tfTabOf(tabsElement).content.props.children, pages[0]);
assert.equal(pages[0].props.locale, "en");

// Switching away unmounts it again.
tabsElement.props.onShowTab("Power");
tabsElement = tabsOf(renderContent());
assert.equal(tabsElement.props.activeTab, "Power");
assert.equal(tfTabOf(tabsElement).content.props.children, null);
assert.equal(tfPages(tabsElement).length, 0);

// Controller locale -> TF locale: only zh-CN is translated, every other
// locale reads the page and its tab label in English.
for (const [controllerLocale, expected] of [
  ["zh-CN", "zh"],
  ["en", "en"],
  ["pt-BR", "en"],
  ["pt-PT", "en"],
]) {
  let current = tabsOf(mountContent(controllerLocale));
  current.props.onShowTab("TF");
  current = tabsOf(renderContent());
  pages = tfPages(current);
  assert.equal(pages.length, 1, `${controllerLocale} mounts one TF page`);
  assert.equal(pages[0].props.locale, expected, `${controllerLocale} renders the TF page as ${expected}`);
  assert.equal(
    tfTabOf(current).title.props["aria-label"],
    tfI18n.translate(expected, "tf.title"),
    `${controllerLocale} labels the TF tab in ${expected}`,
  );
}
assert.notEqual(tfI18n.translate("zh", "tf.title"), tfI18n.translate("en", "tf.title"));

console.log("TF integration tests passed: RPC calls/envelopes and Content tab rendering");
