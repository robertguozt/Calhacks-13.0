import { mutation, query } from "./_generated/server";
import { v } from "convex/values";
import { status } from "./schema";

// Writes come only from the Python server. The deployment URL ships in the browser
// bundle, so every mutation checks a shared secret set with
// `npx convex env set CASE_WRITE_SECRET <value>`.
function auth(secret: string) {
  if (!process.env.CASE_WRITE_SECRET || secret !== process.env.CASE_WRITE_SECRET) {
    throw new Error("forbidden");
  }
}

const signal = v.object({ source: v.string(), fact: v.string(), weight: v.number() });

export const open = mutation({
  args: {
    secret: v.string(),
    member_id: v.string(),
    member_name: v.string(),
    txn_id: v.string(),
    merchant: v.string(),
    category: v.string(),
    amount: v.number(),
    status,
    risk_signals: v.array(signal),
    plan: v.string(),
    decision_rationale: v.string(),
  },
  handler: async (ctx, { secret, ...c }) => {
    auth(secret);
    // idempotent on txn_id: a double poll never opens two cases
    const existing = await ctx.db
      .query("cases")
      .withIndex("by_txn", (q) => q.eq("txn_id", c.txn_id))
      .unique();
    if (existing) return existing._id;
    return await ctx.db.insert("cases", { ...c, opened_at: Date.now() });
  },
});

export const setStatus = mutation({
  args: { secret: v.string(), caseId: v.id("cases"), status },
  handler: async (ctx, { secret, caseId, status }) => {
    auth(secret);
    await ctx.db.patch(caseId, { status });
  },
});

export const addAction = mutation({
  args: { secret: v.string(), caseId: v.id("cases"), type: v.string(), by: v.string(), detail: v.string() },
  handler: async (ctx, { secret, ...a }) => {
    auth(secret);
    await ctx.db.insert("actions", { ...a, t: Date.now() });
  },
});

export const addLine = mutation({
  args: { secret: v.string(), caseId: v.id("cases"), speaker: v.string(), text: v.string() },
  handler: async (ctx, { secret, ...l }) => {
    auth(secret);
    await ctx.db.insert("transcript", { ...l, t: Date.now() });
  },
});

export const reviewOpen = mutation({
  args: { secret: v.string(), trigger: v.string() },
  handler: async (ctx, { secret, trigger }) => {
    auth(secret);
    return await ctx.db.insert("reviews", { started_at: Date.now(), trigger, status: "running", steps: [], summary: "", diff: "" });
  },
});

export const reviewStep = mutation({
  args: { secret: v.string(), reviewId: v.id("reviews"), text: v.string() },
  handler: async (ctx, { secret, reviewId, text }) => {
    auth(secret);
    const r = await ctx.db.get(reviewId);
    if (r && r.steps.length < 200) await ctx.db.patch(reviewId, { steps: [...r.steps, text.slice(0, 500)] });
  },
});

export const reviewSet = mutation({
  args: {
    secret: v.string(),
    reviewId: v.id("reviews"),
    status: v.union(v.literal("running"), v.literal("proposed"), v.literal("approved"), v.literal("rejected"), v.literal("failed")),
    summary: v.optional(v.string()),
    diff: v.optional(v.string()),
  },
  handler: async (ctx, { secret, reviewId, ...patch }) => {
    auth(secret);
    await ctx.db.patch(reviewId, Object.fromEntries(Object.entries(patch).filter(([, x]) => x !== undefined)));
  },
});

export const latestReview = query({
  args: {},
  handler: async (ctx) => (await ctx.db.query("reviews").order("desc").take(1))[0] ?? null,
});

// Demo reset: wipes everything.
export const reset = mutation({
  args: { secret: v.string() },
  handler: async (ctx, { secret }) => {
    auth(secret);
    for (const table of ["actions", "transcript", "cases", "reviews"] as const) {
      for (const doc of await ctx.db.query(table).take(4000)) await ctx.db.delete(doc._id);
    }
  },
});

export const list = query({
  args: {},
  handler: async (ctx) => ctx.db.query("cases").order("desc").take(50),
});

export const get = query({
  args: { caseId: v.id("cases") },
  handler: async (ctx, { caseId }) => {
    const c = await ctx.db.get(caseId);
    if (!c) return null;
    const actions = await ctx.db
      .query("actions")
      .withIndex("by_case_t", (q) => q.eq("caseId", caseId))
      .take(500);
    const transcript = await ctx.db
      .query("transcript")
      .withIndex("by_case_t", (q) => q.eq("caseId", caseId))
      .take(1000);
    return { ...c, actions, transcript };
  },
});
