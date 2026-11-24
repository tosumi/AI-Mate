import { describe, expect, it } from "vitest";
import { loadOpsConfig } from "./config";
import { IDLE_LIMIT_MS, isIdle } from "./idle";
import { formatValue, niceMax } from "./labels";

describe("運営者の画面", () => {
  it("設定が1つでも欠けていれば、使えない", () => {
    const full = { VITE_API_URL: "https://api.example/", VITE_OPS_TENANT_ID: "t", VITE_OPS_CLIENT_ID: "c", VITE_OPS_API_SCOPE: "s" };
    expect(loadOpsConfig(full)).toEqual({ apiUrl: "https://api.example", tenantId: "t", clientId: "c", apiScope: "s" });
    expect(loadOpsConfig({ ...full, VITE_OPS_CLIENT_ID: "" })).toBeNull();
  });

  it("30分操作がなければ、止める", () => {
    expect(IDLE_LIMIT_MS).toBe(30 * 60 * 1000);
    expect(isIdle(0, IDLE_LIMIT_MS - 1)).toBe(false);
    expect(isIdle(0, IDLE_LIMIT_MS)).toBe(true);
  });

  it("グラフの値を、単位つきで出す", () => {
    expect(formatValue(12.345, "percent")).toBe("12.3%");
    expect(formatValue(3 * 1024 * 1024, "bytes")).toBe("3.0 MB");
    expect(formatValue(2048, "mb")).toBe("2.00 GB");
    expect(formatValue(1234, "count")).toBe("1,234");
    expect(formatValue(null, "count")).toBe("—");
    expect([niceMax(0), niceMax(7), niceMax(120), niceMax(0.3)]).toEqual([1, 10, 200, 0.5]);
  });
});
