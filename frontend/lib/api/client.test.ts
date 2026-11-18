import { http, HttpResponse } from "msw";
import { describe, expect, it, vi } from "vitest";
import {
  ApiError,
  archivePeerContact,
  createAccount,
  createPeerContact,
  createPeerEntry,
  deleteAccount,
  deletePeerEntry,
  exportPeerContactStatement,
  getAuthConfig,
  getOverview,
  getPeerContact,
  getPeerSummary,
  invalidIdsFromError,
  listAccounts,
  listCategories,
  listCategoryTree,
  listPeerContacts,
  listPeerEntries,
  listTransactions,
  login,
  logout,
  me,
  patchAccount,
  setAuthFailureHandler,
  settlePeerContact,
  updatePeerContact,
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

    it("fetches overview with spend isolation metrics via getOverview()", async () => {
      const overview = await getOverview({ month: "2026-03" });
      expect(overview.gross_spend_paise).toBe(700000);
      expect(overview.fronted_peer_entries_paise).toBe(300000);
      expect(overview.net_personal_spend_paise).toBe(400000);
      expect(overview.fronted_card_usage_paise).toBe(500000);
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

  describe("Friends & Khata / Peer Ledger Endpoints", () => {
    it("fetches peer summary via getPeerSummary()", async () => {
      server.use(
        http.get("*/api/v1/friends/summary", () => {
          return HttpResponse.json({
            total_receivable_paise: 150000,
            total_payable_paise: 50000,
            net_balance_paise: 100000,
            active_contacts_count: 2,
          });
        }),
      );

      const summary = await getPeerSummary();
      expect(summary.total_receivable_paise).toBe(150000);
      expect(summary.total_payable_paise).toBe(50000);
      expect(summary.net_balance_paise).toBe(100000);
      expect(summary.active_contacts_count).toBe(2);
    });

    it("lists peer contacts with query params via listPeerContacts()", async () => {
      let interceptedUrl = "";
      server.use(
        http.get("*/api/v1/friends/contacts", ({ request }) => {
          interceptedUrl = request.url;
          return HttpResponse.json([
            {
              id: 1,
              name: "Aman",
              phone: "9876543210",
              upi_id: "aman@upi",
              notes: null,
              archived_at: null,
              created_at: "2026-01-01T00:00:00Z",
              balance_paise: 5000,
              total_gave_paise: 5000,
              total_got_paise: 0,
              active_entries_count: 1,
            },
          ]);
        }),
      );

      const contacts = await listPeerContacts(true);
      expect(contacts).toHaveLength(1);
      expect(contacts[0].name).toBe("Aman");
      expect(interceptedUrl).toContain("include_archived=true");
    });

    it("fetches single peer contact via getPeerContact()", async () => {
      server.use(
        http.get("*/api/v1/friends/contacts/:id", ({ params }) => {
          return HttpResponse.json({
            id: Number(params.id),
            name: "Rohit",
            phone: null,
            upi_id: null,
            notes: "Friend",
            archived_at: null,
            created_at: "2026-01-01T00:00:00Z",
            balance_paise: -2000,
            total_gave_paise: 0,
            total_got_paise: 2000,
            active_entries_count: 1,
          });
        }),
      );

      const contact = await getPeerContact(42);
      expect(contact.id).toBe(42);
      expect(contact.name).toBe("Rohit");
      expect(contact.balance_paise).toBe(-2000);
    });

    it("creates, updates, and archives a peer contact", async () => {
      let createdPayload: unknown;
      let updatePayload: unknown;
      let archiveUrl = "";

      server.use(
        http.post("*/api/v1/friends/contacts", async ({ request }) => {
          createdPayload = await request.json();
          return HttpResponse.json({
            id: 10,
            name: "Vikas",
            phone: "9999999999",
            upi_id: null,
            notes: null,
            archived_at: null,
            created_at: "2026-01-01T00:00:00Z",
          });
        }),
        http.patch("*/api/v1/friends/contacts/:id", async ({ request }) => {
          updatePayload = await request.json();
          return HttpResponse.json({
            id: 10,
            name: "Vikas K",
            phone: "9999999999",
            upi_id: null,
            notes: null,
            archived_at: null,
            created_at: "2026-01-01T00:00:00Z",
          });
        }),
        http.delete("*/api/v1/friends/contacts/:id", ({ request }) => {
          archiveUrl = request.url;
          return HttpResponse.json({
            id: 10,
            name: "Vikas K",
            phone: "9999999999",
            upi_id: null,
            notes: null,
            archived_at: "2026-01-02T00:00:00Z",
            created_at: "2026-01-01T00:00:00Z",
          });
        }),
      );

      const created = await createPeerContact({ name: "Vikas", phone: "9999999999" });
      expect(created.id).toBe(10);
      expect(createdPayload).toEqual({ name: "Vikas", phone: "9999999999" });

      const updated = await updatePeerContact(10, { name: "Vikas K" });
      expect(updated.name).toBe("Vikas K");
      expect(updatePayload).toEqual({ name: "Vikas K" });

      const archived = await archivePeerContact(10, true);
      expect(archived.archived_at).toBeTruthy();
      expect(archiveUrl).toContain("confirm_with_balance=true");
    });

    it("settles a contact via settlePeerContact()", async () => {
      let settleBody: unknown;
      server.use(
        http.post("*/api/v1/friends/contacts/:id/settle", async ({ request, params }) => {
          settleBody = await request.json();
          return HttpResponse.json({
            id: 101,
            contact_id: Number(params.id),
            transaction_id: null,
            date: "2026-01-05",
            amount_paise: 5000,
            direction: "you_got",
            is_settlement: true,
            notes: "Settlement",
            created_at: "2026-01-05T00:00:00Z",
          });
        }),
      );

      const entry = await settlePeerContact(5, { notes: "Paid via UPI" });
      expect(entry.id).toBe(101);
      expect(entry.is_settlement).toBe(true);
      expect(settleBody).toEqual({ notes: "Paid via UPI" });
    });

    it("creates, lists, and deletes peer entries", async () => {
      let createdEntryPayload: unknown;
      let deletedEntryId = 0;

      server.use(
        http.get("*/api/v1/friends/contacts/:id/entries", ({ params }) => {
          return HttpResponse.json([
            {
              id: 50,
              contact_id: Number(params.id),
              transaction_id: 12,
              date: "2026-01-01",
              amount_paise: 120000,
              direction: "you_gave",
              is_settlement: false,
              notes: "Dinner split",
              created_at: "2026-01-01T00:00:00Z",
            },
          ]);
        }),
        http.post("*/api/v1/friends/entries", async ({ request }) => {
          createdEntryPayload = await request.json();
          return HttpResponse.json({
            id: 51,
            contact_id: 2,
            transaction_id: 12,
            date: "2026-01-01",
            amount_paise: 120000,
            direction: "you_gave",
            is_settlement: false,
            notes: "Dinner split",
            created_at: "2026-01-01T00:00:00Z",
          });
        }),
        http.delete("*/api/v1/friends/entries/:id", ({ params }) => {
          deletedEntryId = Number(params.id);
          return new HttpResponse(null, { status: 204 });
        }),
      );

      const entries = await listPeerEntries(2);
      expect(entries).toHaveLength(1);
      expect(entries[0].amount_paise).toBe(120000);

      const created = await createPeerEntry({
        contact_id: 2,
        transaction_id: 12,
        amount_paise: 120000,
        direction: "you_gave",
        notes: "Dinner split",
      });
      expect(created.id).toBe(51);
      expect(createdEntryPayload).toEqual({
        contact_id: 2,
        transaction_id: 12,
        amount_paise: 120000,
        direction: "you_gave",
        notes: "Dinner split",
      });

      const delRes = await deletePeerEntry(51);
      expect(delRes).toBeUndefined();
      expect(deletedEntryId).toBe(51);
    });

    it("exports contact statement CSV via exportPeerContactStatement()", async () => {
      server.use(
        http.get("*/api/v1/friends/contacts/:id/export", () => {
          return new HttpResponse("Date,Description,Direction,Amount (INR),Settlement Status,Running Balance (INR)\n2026-03-01,Test,you_gave,10.00,Regular,10.00", {
            headers: {
              "content-type": "text/csv; charset=utf-8",
              "content-disposition": 'attachment; filename="peer_contact_5_statement.csv"',
            },
          });
        }),
      );

      const res = await exportPeerContactStatement(5);
      expect(res.filename).toBe("peer_contact_5_statement.csv");
      expect(res.csvText).toContain("Date,Description,Direction");
      expect(res.csvText).toContain("2026-03-01,Test,you_gave,10.00,Regular,10.00");
    });
  });

  describe("invalidIdsFromError", () => {
    it("invalidIdsFromError reads invalid_ids from a 422 body", () => {
      const err = new ApiError(422, "x", { detail: { message: "m", invalid_ids: [4, 9] } });
      expect(invalidIdsFromError(err)).toEqual([4, 9]);
    });

    it("returns [] for another status or error shape", () => {
      const body = { detail: { invalid_ids: [4] } };
      expect(invalidIdsFromError(new ApiError(400, "x", body))).toEqual([]);
      expect(invalidIdsFromError(new ApiError(422, "x", { detail: "nope" }))).toEqual([]);
      expect(invalidIdsFromError(new Error("boom"))).toEqual([]);
    });
  });
});
