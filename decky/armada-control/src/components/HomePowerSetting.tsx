import { toaster } from "@decky/api";
import { useEffect, useState } from "react";
import { getHomePower, setHomePower } from "../backend";
import { t } from "../i18n";
import type { TranslationKey } from "../i18n";
import type { HomePowerState } from "../types";
import { ToggleRow } from "./widgets";

// Stable backend reason codes that have a user-facing explanation.
const KNOWN_FAILURES: Record<string, TranslationKey> = {
  "restore-failed": "settings.homePowerRestoreFailed",
  "settings-write-failed": "settings.homePowerSettingsWriteFailed",
  "invalid-request-property": "settings.homePowerInvalidRequestProperty",
};

function failureText(error: unknown): string {
  const raw = String(error);
  for (const [code, key] of Object.entries(KNOWN_FAILURES)) {
    if (raw.includes(code)) return t(key);
  }
  return raw;
}

export function HomePowerSetting() {
  const [state, setState] = useState<HomePowerState | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    let cancelled = false;
    getHomePower()
      .then((next) => { if (!cancelled) setState(next); })
      .catch((error) => {
        if (!cancelled) toaster.toast({ title: t("settings.homePowerLoadError"), body: failureText(error) });
      })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, []);

  const apply = async (enabled: boolean) => {
    if (!state || loading || saving) return;
    setSaving(true);
    try {
      setState(await setHomePower(enabled));
    } catch (error) {
      toaster.toast({ title: t("settings.homePowerError"), body: failureText(error) });
      try {
        setState(await getHomePower());
      } catch (refreshError) {
        console.warn("[armada-control] home power state refresh failed", refreshError);
      }
    } finally {
      setSaving(false);
    }
  };

  // A failed restore leaves the preference off while the limit is still live.
  const checked = !!state && (state.enabled || state.reason === "restore-failed");
  // Unsupported only blocks turning it on; a displayed-on state can still be turned off.
  const blocked = !!state && !state.supported && !checked;
  return (
    <ToggleRow
      label={t("settings.homePower")}
      value={checked}
      disabled={loading || saving || !state || blocked}
      onChange={(enabled) => { void apply(enabled); }}
    />
  );
}
