import { http, HttpResponse } from "msw";
import { describe, expect, it, vi } from "vitest";
import {
  ApiError,
  createAccount,
  deleteAccount,
  getAuthConfig,
  listAccounts,
  listCategories,
  listCategoryTree,
  listTransactions,
  login,
  logout,
  me,
  patchAccount,
  setAuthFailureHandler,
} from "./client";
import { server } from "@/tests/mocks/server";
import { mockAccounts, mockUser } from "@/tests/mocks/handlers";

describe("API Client (lib/api/client.ts)", () => {
  describe("Happy Path Requests", () => {
    it("fetches current user via me()", async () => {
      const user = await me();
      expect(user).toEqual(mockUser);
    });

    it("fetches accounts list", async () => {
      const accounts = await listAccounts();
      expect(accounts).toEqual(mockAccounts);
    });

    it("creates an account via POST", async () => {
      const created = await createAccount({
        name: "Emergency Fund",
        type: "bank",
      });
      expect(created.id).toBe(99);
      expect(created.name).toBe("Emergency Fund");
    });

    it("patches an account via PATCH", async () => {
      const patched = await patchAccount(1, { name: "Updated Salary Bank" });
      expect(patched.name).toBe("Updated Salary Bank");
    });

    it("deletes an account returning undefined on 204 No Content", async () => {
      const res = await deleteAccount(1);
      expect(res).toBeUndefined();
    });
  });

  describe("Query Parameter Serialization", () => {
    it("serializes listTransactions parameters including repeated list-valued keys", async () => {
      let interceptedUrl = "";

      server.use(
        http.get("*/api/v1/transactions", ({ request }) => {
          interceptedUrl = request.url;
          return HttpResponse.json([]);
        }),
      );

      await listTransactions({
        transaction_type: ["spend", "income"],
        amount_sign: "positive",
        account_id: 10,
        category_id: 20,
        label_id: 30,
        date_from: "2026-08-01",
        date_to: "2026-08-31",
        limit: 50,
        offset: 100,
      });

      const url = new URL(interceptedUrl);
      expect(url.searchParams.getAll("transaction_type")).toEqual(["spend", "income"]);
      expect(url.searchParams.get("amount_sign")).toBe("positive");
      expect(url.searchParams.get("account_id")).toBe("10");
      expect(url.searchParams.get("category_id")).toBe("20");
      expect(url.searchParams.get("label_id")).toBe("30");
      expect(url.searchParams.get("date_from")).toBe("2026-08-01");
      expect(url.searchParams.get("date_to")).toBe("2026-08-31");
      expect(url.searchParams.get("limit")).toBe("50");
      expect(url.searchParams.get("offset")).toBe("100");
    });

    it("serializes listCategories and listCategoryTree parameters", async () => {
      let categoryUrl = "";
      let treeUrl = "";

      server.use(
        http.get("*/api/v1/categories", ({ request }) => {
          const url = new URL(request.url);
          if (url.searchParams.get("tree") === "true") {
            treeUrl = request.url;
          } else {
            categoryUrl = request.url;
          }
          return HttpResponse.json([]);
        }),
      );

      await listCategories({ kind: "spend" });
      const catParsed = new URL(categoryUrl);
      expect(catParsed.searchParams.get("kind")).toBe("spend");
      expect(catParsed.searchParams.get("tree")).toBeNull();

      await listCategoryTree({ kind: "income" });
      const treeParsed = new URL(treeUrl);
      expect(treeParsed.searchParams.get("kind")).toBe("income");
      expect(treeParsed.searchParams.get("tree")).toBe("true");
    });
  });

  describe("Error Parsing & ApiError", () => {
    it("extracts simple string detail from error responses", async () => {
      server.use(
        http.get("*/api/v1/accounts", () => {
          return HttpResponse.json({ detail: "Database unavailable" }, { status: 503 });
        }),
      );

      await expect(listAccounts()).rejects.toThrow(ApiError);
      try {
        await listAccounts();
      } catch (err) {
        expect(err).toBeInstanceOf(ApiError);
        const apiErr = err as ApiError;
        expect(apiErr.status).toBe(503);
        expect(apiErr.detail).toBe("Database unavailable");
      }
    });

    it("extracts first message from FastAPI 422 validation array", async () => {
      server.use(
        http.post("*/api/v1/accounts", () => {
          return HttpResponse.json(
            {
              detail: [
                { loc: ["body", "name"], msg: "Field required", type: "missing" },
              ],
            },
            { status: 422 },
          );
        }),
      );

      try {
        await createAccount({ name: "", type: "bank" });
      } catch (err) {
        expect(err).toBeInstanceOf(ApiError);
        const apiErr = err as ApiError;
        expect(apiErr.status).toBe(422);
        expect(apiErr.detail).toBe("Field required");
      }
    });

    it("preserves structured JSON detail body on ApiError.body", async () => {
      const customBody = {
        detail: {
          message: "Batch commit failed",
          invalid_ids: [101, 102],
        },
      };

      server.use(
        http.get("*/api/v1/accounts", () => {
          return HttpResponse.json(customBody, { status: 400 });
        }),
      );

      try {
        await listAccounts();
      } catch (err) {
        expect(err).toBeInstanceOf(ApiError);
        const apiErr = err as ApiError;
        expect(apiErr.status).toBe(400);
        expect(apiErr.body).toEqual(customBody);
      }
    });

    it("handles non-JSON error responses gracefully", async () => {
      server.use(
        http.get("*/api/v1/accounts", () => {
          return new HttpResponse("502 Bad Gateway Server Error", {
            status: 502,
            statusText: "Bad Gateway",
            headers: { "content-type": "text/plain" },
          });
        }),
      );

      try {
        await listAccounts();
      } catch (err) {
        expect(err).toBeInstanceOf(ApiError);
        const apiErr = err as ApiError;
        expect(apiErr.status).toBe(502);
        expect(apiErr.detail).toBe("Bad Gateway");
        expect(apiErr.body).toBeUndefined();
      }
    });
  });

  describe("Auth Session & Silent 401 Refresh Interceptor", () => {
    it("transparently refreshes token on 401 and replays the original request", async () => {
      let accountsCallCount = 0;
      let refreshCallCount = 0;

      server.use(
        http.get("*/api/v1/accounts", () => {
          accountsCallCount++;
          if (accountsCallCount === 1) {
            return HttpResponse.json({ detail: "Token expired" }, { status: 401 });
          }
          return HttpResponse.json(mockAccounts);
        }),
        http.post("*/api/v1/auth/refresh", () => {
          refreshCallCount++;
          return HttpResponse.json({ ok: true });
        }),
      );

      const accounts = await listAccounts();
      expect(accounts).toEqual(mockAccounts);
      expect(accountsCallCount).toBe(2);
      expect(refreshCallCount).toBe(1);
    });

    it("single-flights concurrent 401s into a single /auth/refresh request", async () => {
      let accountsCalls = 0;
      let categoriesCalls = 0;
      let refreshCalls = 0;

      server.use(
        http.get("*/api/v1/accounts", () => {
          accountsCalls++;
          if (accountsCalls === 1) {
            return HttpResponse.json({ detail: "Token expired" }, { status: 401 });
          }
          return HttpResponse.json(mockAccounts);
        }),
        http.get("*/api/v1/categories", () => {
          categoriesCalls++;
          if (categoriesCalls === 1) {
            return HttpResponse.json({ detail: "Token expired" }, { status: 401 });
          }
          return HttpResponse.json([]);
        }),
        http.post("*/api/v1/auth/refresh", async () => {
          refreshCalls++;
          // Add small delay to allow concurrent requests to coalesce
          await new Promise((r) => setTimeout(r, 10));
          return HttpResponse.json({ ok: true });
        }),
      );

      const [accounts, categories] = await Promise.all([
        listAccounts(),
        listCategories(),
      ]);

      expect(accounts).toEqual(mockAccounts);
      expect(categories).toEqual([]);
      expect(accountsCalls).toBe(2);
      expect(categoriesCalls).toBe(2);
      expect(refreshCalls).toBe(1); // Crucial single-flight invariant
    });

    it("invokes onAuthFailure and rejects when /auth/refresh fails", async () => {
      const onAuthFailureSpy = vi.fn();
      const unregister = setAuthFailureHandler(onAuthFailureSpy);

      try {
        server.use(
          http.get("*/api/v1/accounts", () => {
            return HttpResponse.json({ detail: "Token expired" }, { status: 401 });
          }),
          http.post("*/api/v1/auth/refresh", () => {
            return HttpResponse.json({ detail: "Refresh token revoked" }, { status: 401 });
          }),
        );

        await expect(listAccounts()).rejects.toThrow(ApiError);
        expect(onAuthFailureSpy).toHaveBeenCalledTimes(1);
      } finally {
        unregister();
      }
    });

    it("invokes onAuthFailure when 401 persists even after successful refresh", async () => {
      const onAuthFailureSpy = vi.fn();
      const unregister = setAuthFailureHandler(onAuthFailureSpy);

      try {
        server.use(
          http.get("*/api/v1/accounts", () => {
            return HttpResponse.json({ detail: "Unauthorized" }, { status: 401 });
          }),
          http.post("*/api/v1/auth/refresh", () => {
            return HttpResponse.json({ ok: true });
          }),
        );

        await expect(listAccounts()).rejects.toThrow(ApiError);
        expect(onAuthFailureSpy).toHaveBeenCalledTimes(1);
      } finally {
        unregister();
      }
    });

    it("bypasses refresh on endpoints configured with noRefresh: true (e.g. login, logout, getAuthConfig)", async () => {
      let refreshCalls = 0;
      server.use(
        http.post("*/api/v1/auth/login", () => {
          return HttpResponse.json({ detail: "Invalid credentials" }, { status: 401 });
        }),
        http.post("*/api/v1/auth/refresh", () => {
          refreshCalls++;
          return HttpResponse.json({ ok: true });
        }),
      );

      await expect(login("test@example.com", "wrongpass")).rejects.toThrow(ApiError);
      expect(refreshCalls).toBe(0); // Did not trigger refresh
    });
  });
});
