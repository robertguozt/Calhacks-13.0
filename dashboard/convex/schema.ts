import { defineSchema, defineTable } from "convex/server";
import { v } from "convex/values";

export const status = v.union(
  v.literal("allowed"), // triage let it through, no hold
  v.literal("open"), // held, waiting on the member / analyst
  v.literal("released"),
  v.literal("escalated"),
  v.literal("confirmed_fraud"),
);

export default defineSchema({
  cases: defineTable({
    member_id: v.string(),
    member_name: v.string(),
    txn_id: v.string(),
    merchant: v.string(),
    category: v.string(),
    amount: v.number(),
    opened_at: v.number(), // epoch ms
    status,
    risk_signals: v.array(v.object({ source: v.string(), fact: v.string(), weight: v.number() })),
    plan: v.string(),
    decision_rationale: v.string(),
  }).index("by_txn", ["txn_id"]),

  // append-only logs, split out so appends don't rewrite the case doc
  actions: defineTable({
    caseId: v.id("cases"),
    t: v.number(),
    type: v.string(), // hold | call | family_call | release | escalate | verify | deny ...
    by: v.string(), // agent | member | human | gate
    detail: v.string(),
  }).index("by_case_t", ["caseId", "t"]),

  // Morning review: a Factory Droid proposes a policy diff; a human approves it
  reviews: defineTable({
    started_at: v.number(),
    trigger: v.string(), // schedule | analyst
    status: v.union(v.literal("running"), v.literal("proposed"), v.literal("approved"), v.literal("rejected"), v.literal("failed")),
    steps: v.array(v.string()), // Droid's visible steps (bounded)
    summary: v.string(),
    diff: v.string(), // unified diff of policy.md; empty if no change proposed
  }),

  transcript: defineTable({
    caseId: v.id("cases"),
    t: v.number(),
    speaker: v.string(), // agent | member
    text: v.string(),
  }).index("by_case_t", ["caseId", "t"]),
});
