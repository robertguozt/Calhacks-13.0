"use client";

import { useQuery } from "convex/react";
import { anyApi } from "convex/server";
import { useEffect, useRef, useState } from "react";

// Untyped so this builds without codegen; switch to `import { api } from "@/convex/_generated/api"` after `npx convex dev`.
const api = anyApi;
const SERVER = process.env.NEXT_PUBLIC_SERVER_URL;

type Status = "allowed" | "open" | "released" | "escalated" | "confirmed_fraud";
type Signal = { source: string; fact: string; weight: number };
type Case = {
  _id: string;
  member_name: string;
  merchant: string;
  category: string;
  amount: number;
  opened_at: number;
  status: Status;
  risk_signals: Signal[];
  plan: string;
  decision_rationale: string;
};
type Action = { _id: string; t: number; type: string; by: string; detail: string };
type Line = { _id: string; t: number; speaker: string; text: string };
type CaseDetail = Case & { actions: Action[]; transcript: Line[] };

const money = (n: number) =>
  n.toLocaleString("en-US", { style: "currency", currency: "USD", minimumFractionDigits: 0 });
const clock = (t: number) => new Date(t).toLocaleTimeString([], { hour: "numeric", minute: "2-digit", second: "2-digit" });

// Returns null on success, otherwise the error text to show.
async function post(path: string, body?: unknown): Promise<string | null> {
  if (!SERVER) return "NEXT_PUBLIC_SERVER_URL is not set";
  try {
    const res = await fetch(`${SERVER}${path}`, {
      method: "POST",
      // ngrok's free tier serves an HTML interstitial to browsers unless this header is present
      headers: { "ngrok-skip-browser-warning": "1", ...(body !== undefined && { "Content-Type": "application/json" }) },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    return res.ok ? null : `${res.status}: ${await res.text()}`;
  } catch (e) {
    return String(e);
  }
}

const BADGE: Record<Status, [string, string]> = {
  allowed: ["ALLOWED", "bg-stone-200 text-stone-700"],
  open: ["HELD", "bg-amber-100 text-amber-900 ring-1 ring-amber-400"],
  released: ["RELEASED", "bg-emerald-100 text-emerald-900"],
  escalated: ["ESCALATED", "bg-red-100 text-red-800 ring-1 ring-red-300"],
  confirmed_fraud: ["FRAUD", "bg-red-700 text-white"],
};

function Badge({ status }: { status: Status }) {
  const [label, cls] = BADGE[status] ?? [status, "bg-stone-200"];
  return <span className={`rounded-md px-2.5 py-1 text-sm font-semibold tracking-wide ${cls}`}>{label}</span>;
}

function Panel({ title, children, className = "" }: { title: string; children: React.ReactNode; className?: string }) {
  return (
    <section className={`rounded-xl border border-stone-200 bg-white p-5 shadow-sm ${className}`}>
      <h2 className="mb-4 text-sm font-semibold uppercase tracking-wider text-stone-500">{title}</h2>
      {children}
    </section>
  );
}

export default function Page() {
  const cases = useQuery(api.cases.list) as Case[] | undefined;
  const [selected, setSelected] = useState<string | null>(null);

  // Auto-select the newest case whenever a new one arrives (adjusting state during render, no effect needed).
  const newestId = cases?.[0]?._id ?? null;
  const [seenNewest, setSeenNewest] = useState<string | null>(null);
  if (newestId !== seenNewest) {
    setSeenNewest(newestId);
    setSelected(newestId);
  }

  const [resetErr, setResetErr] = useState<string | null>(null);
  async function reset() {
    if (!confirm("Reset the demo? This wipes all cases.")) return;
    setResetErr(await post("/reset"));
  }

  return (
    <div className="mx-auto max-w-[1600px] px-6 py-6">
      <header className="mb-6 flex items-end justify-between gap-4">
        <div>
          <h1 className="text-3xl font-semibold tracking-tight">Second Signature</h1>
          <p className="mt-1 text-lg text-stone-500">Bayview Community CU · after-hours fraud desk</p>
        </div>
        <div className="text-right">
          <button
            onClick={reset}
            className="rounded-lg border border-stone-300 bg-white px-3 py-1.5 text-sm text-stone-600 hover:bg-stone-50"
          >
            Reset demo
          </button>
          {resetErr && <p className="mt-1 max-w-xs text-sm text-red-700">{resetErr}</p>}
        </div>
      </header>

      <div className="grid gap-6 xl:grid-cols-[290px_minmax(0,1fr)_370px]">
        <Panel title="Cases">
          {cases === undefined ? (
            <p className="text-stone-500">Loading…</p>
          ) : cases.length === 0 ? (
            <p className="text-stone-500">No cases yet. Run a scenario.</p>
          ) : (
            <ul className="-mx-2 max-h-[70vh] space-y-1 overflow-y-auto">
              {cases.map((c) => (
                <li key={c._id}>
                  <button
                    onClick={() => setSelected(c._id)}
                    className={`w-full rounded-lg px-3 py-3 text-left transition ${
                      c._id === selected ? "bg-stone-900 text-white" : "hover:bg-stone-100"
                    }`}
                  >
                    <div className="flex items-center justify-between gap-2">
                      <span className="truncate text-lg font-medium">{c.member_name}</span>
                      <Badge status={c.status} />
                    </div>
                    <div className={`mt-1 flex justify-between text-base ${c._id === selected ? "text-stone-300" : "text-stone-500"}`}>
                      <span className="truncate">{c.merchant}</span>
                      <span className="font-mono">{money(c.amount)}</span>
                    </div>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </Panel>

        <CaseView caseId={selected} />

        <div className="flex flex-col gap-6">
          <ScenarioForm />
          <MorningReview />
        </div>

        <PermissionMatrix />
        <RealVsSimulated />
      </div>
    </div>
  );
}

function CaseView({ caseId }: { caseId: string | null }) {
  const c = useQuery(api.cases.get, caseId ? { caseId } : "skip") as CaseDetail | null | undefined;
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const scroller = useRef<HTMLDivElement>(null);

  const timeline = c
    ? [
        ...c.actions.map((a) => ({ t: a.t, key: a._id, action: a })),
        ...c.transcript.map((l) => ({ t: l.t, key: l._id, line: l })),
      ].sort((a, b) => a.t - b.t)
    : [];

  useEffect(() => {
    scroller.current?.scrollTo({ top: scroller.current.scrollHeight, behavior: "smooth" });
  }, [timeline.length, caseId]);

  async function act(path: "release" | "confirm_fraud") {
    setBusy(true);
    setErr(await post(`/cases/${caseId}/${path}`));
    setBusy(false);
  }

  if (!caseId || c === null) return <Panel title="Case"><p className="text-stone-500">Select a case.</p></Panel>;
  if (c === undefined) return <Panel title="Case"><p className="text-stone-500">Loading…</p></Panel>;

  return (
    <Panel title="Case">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <p className="text-lg text-stone-500">{c.member_name}</p>
          <p className="text-3xl font-semibold">
            {c.merchant} <span className="font-mono">{money(c.amount)}</span>
          </p>
          <p className="mt-1 text-base text-stone-500">
            {c.category.replace(/_/g, " ")} · opened {clock(c.opened_at)}
          </p>
        </div>
        <Badge status={c.status} />
      </div>

      {(c.status === "open" || c.status === "escalated") && (
        <div className="mt-5 flex flex-wrap items-center gap-3">
          <button
            disabled={busy}
            onClick={() => act("release")}
            className="rounded-lg bg-stone-900 px-4 py-2 text-base font-medium text-white hover:bg-stone-700 disabled:opacity-50"
          >
            Release (analyst)
          </button>
          <button
            disabled={busy}
            onClick={() => act("confirm_fraud")}
            className="rounded-lg border border-red-300 bg-white px-4 py-2 text-base font-medium text-red-700 hover:bg-red-50 disabled:opacity-50"
          >
            Confirm fraud
          </button>
        </div>
      )}
      {err && <pre className="mt-3 whitespace-pre-wrap rounded-lg bg-red-50 p-3 font-mono text-sm text-red-800">{err}</pre>}

      <div className="mt-6 grid gap-5 lg:grid-cols-2">
        <div>
          <h3 className="mb-1 text-sm font-semibold uppercase tracking-wider text-stone-500">Plan</h3>
          <p className="text-base leading-relaxed">{c.plan || "—"}</p>
        </div>
        <div>
          <h3 className="mb-1 text-sm font-semibold uppercase tracking-wider text-stone-500">Decision rationale</h3>
          <p className="text-base leading-relaxed">{c.decision_rationale || "—"}</p>
        </div>
      </div>

      <h3 className="mb-2 mt-6 text-sm font-semibold uppercase tracking-wider text-stone-500">Risk signals</h3>
      <ul className="space-y-2">
        {c.risk_signals.map((s, i) => (
          <li key={i} className="grid grid-cols-[72px_1fr_96px] items-center gap-3">
            <span className="rounded bg-stone-100 px-2 py-0.5 text-center font-mono text-sm text-stone-600">{s.source}</span>
            <span className="text-base">{s.fact}</span>
            {/* ponytail: assumes weight in 0..1; clamped so an out-of-range value can't overflow the bar */}
            <span className="h-2 overflow-hidden rounded-full bg-stone-100" title={String(s.weight)}>
              <span
                className="block h-full rounded-full bg-amber-500"
                style={{ width: `${Math.max(0, Math.min(1, s.weight)) * 100}%` }}
              />
            </span>
          </li>
        ))}
        {c.risk_signals.length === 0 && <li className="text-stone-500">None recorded.</li>}
      </ul>

      <h3 className="mb-2 mt-6 text-sm font-semibold uppercase tracking-wider text-stone-500">Timeline</h3>
      <div ref={scroller} className="max-h-[55vh] space-y-2 overflow-y-auto rounded-lg border border-stone-200 bg-stone-50 p-3">
        {timeline.length === 0 && <p className="text-stone-500">Nothing yet.</p>}
        {timeline.map((item) =>
          "action" in item ? (
            <ActionRow key={item.key} a={item.action} />
          ) : (
            <div key={item.key} className={`flex gap-3 ${item.line.speaker === "agent" ? "" : "justify-end"}`}>
              <div
                className={`max-w-[80%] rounded-xl px-4 py-2 text-base ${
                  item.line.speaker === "agent" ? "bg-white ring-1 ring-stone-200" : "bg-sky-50 ring-1 ring-sky-200"
                }`}
              >
                <span className="mr-2 text-sm font-semibold text-stone-500">{item.line.speaker}</span>
                {item.line.text}
                <span className="ml-2 text-xs text-stone-400">{clock(item.t)}</span>
              </div>
            </div>
          ),
        )}
      </div>
    </Panel>
  );
}

function ActionRow({ a }: { a: Action }) {
  if (a.by === "gate")
    return (
      <div className="rounded-lg border-l-4 border-red-600 bg-red-50 px-4 py-2.5">
        <p className="text-sm font-bold uppercase tracking-wide text-red-700">
          {a.type === "override" ? "Code floor" : "Permission gate"} · {a.type}
          <span className="ml-2 font-normal normal-case text-red-400">{clock(a.t)}</span>
        </p>
        <p className="text-base text-red-900">{a.detail}</p>
      </div>
    );
  if (a.type === "family_bridge" || a.type === "connect_family")
    return (
      <div className="rounded-lg border-l-4 border-orange-400 bg-orange-50 px-4 py-2.5">
        <p className="text-sm font-bold uppercase tracking-wide text-orange-800">
          <span className="mr-2 rounded bg-orange-200 px-1.5 py-0.5 text-xs">family</span>
          {a.type.replace("_", " ")} · {a.by}
          <span className="ml-2 font-normal normal-case text-orange-400">{clock(a.t)}</span>
        </p>
        <p className="text-base text-orange-950">{a.detail}</p>
      </div>
    );
  return (
    <div className="flex items-baseline gap-3 px-1 py-1 text-base">
      <span className="text-xs text-stone-400">{clock(a.t)}</span>
      <span className="rounded bg-stone-200 px-2 py-0.5 font-mono text-sm text-stone-700">{a.type}</span>
      <span className="text-sm text-stone-500">{a.by}</span>
      <span>{a.detail}</span>
    </div>
  );
}

const CATEGORIES = ["gift_card", "electronics", "wire_transfer", "crypto", "groceries", "pharmacy", "sporting_goods", "restaurant", "other"];
type Scenario = { member_id: string; merchant: string; category: string; amount: string; note: string };
const PRESETS: [string, Scenario][] = [
  ["$500 Target gift cards (Dolores)", { member_id: "dolores", merchant: "Target Gift Cards", category: "gift_card", amount: "500", note: "" }],
  ["$300 Bay Cycles bike (Dolores)", { member_id: "dolores", merchant: "Bay Cycles", category: "sporting_goods", amount: "300", note: "" }],
  ["$100 gift card (Robert)", { member_id: "robert", merchant: "Target Gift Cards", category: "gift_card", amount: "100", note: "" }],
];

function ScenarioForm() {
  const [f, setF] = useState<Scenario>(PRESETS[0][1]);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const set = (k: keyof Scenario) => (e: { target: { value: string } }) => setF({ ...f, [k]: e.target.value });
  const field = "mt-1 w-full rounded-lg border border-stone-300 bg-white px-3 py-2 text-base";

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    const err = await post("/scenario", { ...f, amount: parseInt(f.amount, 10) });
    setMsg(err ? { ok: false, text: err } : { ok: true, text: "Sent. Watch the case list." });
    setBusy(false);
  }

  return (
    <Panel title="Judge scenario">
      <div className="mb-4 flex flex-col gap-2">
        {PRESETS.map(([label, s]) => (
          <button
            key={label}
            type="button"
            onClick={() => setF(s)}
            className="rounded-lg border border-stone-300 px-3 py-2 text-left text-sm hover:bg-stone-50"
          >
            {label}
          </button>
        ))}
      </div>
      <form onSubmit={submit} className="space-y-3">
        <label className="block text-sm font-medium text-stone-600">
          Member
          <select value={f.member_id} onChange={set("member_id")} className={field}>
            <option value="dolores">Dolores Whitcomb, 74</option>
            <option value="robert">Robert Chen, 71</option>
          </select>
        </label>
        <label className="block text-sm font-medium text-stone-600">
          Merchant
          <input required value={f.merchant} onChange={set("merchant")} className={field} />
        </label>
        <div className="grid grid-cols-2 gap-3">
          <label className="block text-sm font-medium text-stone-600">
            Category
            <select value={f.category} onChange={set("category")} className={field}>
              {CATEGORIES.map((c) => (
                <option key={c} value={c}>
                  {c}
                </option>
              ))}
            </select>
          </label>
          <label className="block text-sm font-medium text-stone-600">
            Amount ($)
            <input required type="number" min={1} step={1} value={f.amount} onChange={set("amount")} className={field} />
          </label>
        </div>
        <label className="block text-sm font-medium text-stone-600">
          Note <span className="font-normal text-stone-400">(optional)</span>
          <input value={f.note} onChange={set("note")} className={field} />
        </label>
        <button
          disabled={busy}
          className="w-full rounded-lg bg-stone-900 px-4 py-2.5 text-base font-medium text-white hover:bg-stone-700 disabled:opacity-50"
        >
          {busy ? "Sending…" : "Run scenario"}
        </button>
        {msg && (
          <pre className={`whitespace-pre-wrap rounded-lg p-3 font-mono text-sm ${msg.ok ? "bg-emerald-50 text-emerald-800" : "bg-red-50 text-red-800"}`}>
            {msg.text}
          </pre>
        )}
      </form>
    </Panel>
  );
}

const MATRIX: [string, string, string, string][] = [
  ["Read transactions, identity signals", "✅", "", ""],
  ["Place a hold on a pending purchase", "✅ (max 1 business day)", "", ""],
  ["Call the member; verify identity first", "✅", "", ""],
  ["Listen to calls on her protected line (callers are told)", "✅ (opted in; memory only)", "", ""],
  ["Whisper a scam warning only she can hear", "✅", "", ""],
  ["Call the trusted family contact", "Only on red flags or 2 unanswered tries", "", ""],
  ["Connect the member live to her trusted contact (agent then leaves the call)", "", "✅ (verified, her choice)", ""],
  ["Release a hold under $1,000 (not a first-ever gift card, wire or crypto)", "", "✅ (after verification)", ""],
  ["Release a hold of $1,000+", "", "", "✅"],
  ["Move, refund, or send money", "❌ never", "❌", "✅ only"],
  ["Close or freeze the account", "❌ never", "", "✅ only"],
];

function PermissionMatrix() {
  return (
    <Panel title="Permission matrix" className="xl:col-span-2">
      <table className="w-full text-left text-base">
        <thead>
          <tr className="border-b border-stone-200 text-sm text-stone-500">
            <th className="py-2 pr-4 font-semibold">Action</th>
            <th className="py-2 pr-4 font-semibold">Agent alone</th>
            <th className="py-2 pr-4 font-semibold">Needs member yes</th>
            <th className="py-2 font-semibold">Needs human analyst</th>
          </tr>
        </thead>
        <tbody>
          {MATRIX.map((row) => (
            <tr key={row[0]} className="border-b border-stone-100 last:border-0">
              {row.map((cell, i) => (
                <td key={i} className={`py-2.5 pr-4 ${i === 0 ? "font-medium" : ""} ${cell.startsWith("❌") ? "text-red-700" : ""}`}>
                  {cell}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      <p className="mt-3 text-sm text-stone-500">
        Enforced in code (<code className="font-mono">server/gate.py</code>), not in the prompt.
      </p>
    </Panel>
  );
}

function RealVsSimulated() {
  const real = [
    "Live phone calls (Twilio), voice and speech-to-text (Cartesia Sonic + Ink), reasoning (Claude)",
    "CRS sandbox calls against test identities",
    "Holds and the permission gate enforced in our code",
  ];
  const sim = [
    "The credit union, and Dolores (a CRS test identity)",
    "Nessie is Capital One's sandbox bank, not a real core",
    "The scammer (a teammate)",
    "Holds don't reach a card network",
  ];
  return (
    <Panel title="Real / simulated">
      <div className="grid grid-cols-2 gap-4 text-base">
        <div>
          <h3 className="mb-2 font-semibold text-emerald-800">Real</h3>
          <ul className="list-disc space-y-1.5 pl-5">{real.map((r) => <li key={r}>{r}</li>)}</ul>
        </div>
        <div>
          <h3 className="mb-2 font-semibold text-stone-600">Simulated</h3>
          <ul className="list-disc space-y-1.5 pl-5">{sim.map((r) => <li key={r}>{r}</li>)}</ul>
        </div>
      </div>
    </Panel>
  );
}

type Review = {
  started_at: number;
  trigger: string;
  status: "running" | "proposed" | "approved" | "rejected" | "failed";
  steps: string[];
  summary: string;
  diff: string;
};

function MorningReview() {
  const r = useQuery(api.cases.latestReview) as Review | null | undefined;
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const log = useRef<HTMLPreElement>(null);

  useEffect(() => {
    log.current?.scrollTo({ top: log.current.scrollHeight });
  }, [r?.steps.length]);

  async function send(path: string) {
    setBusy(true);
    setErr(await post(path));
    setBusy(false);
  }

  const diffLine = (l: string) =>
    l.startsWith("+") && !l.startsWith("+++") ? "bg-emerald-50 text-emerald-800"
    : l.startsWith("-") && !l.startsWith("---") ? "bg-red-50 text-red-800"
    : l.startsWith("@@") ? "text-stone-400"
    : "text-stone-700";

  return (
    <Panel title="Morning review">
      <p className="-mt-2 mb-4 text-sm text-stone-500">
        Factory Droid reads last night&apos;s case files and proposes a policy change. A human approves it.
      </p>
      <button
        disabled={busy || r?.status === "running"}
        onClick={() => send("/review")}
        className="w-full rounded-lg border border-stone-300 bg-white px-4 py-2 text-base font-medium hover:bg-stone-50 disabled:opacity-50"
      >
        {r?.status === "running" ? "Review running…" : "Run review now"}
      </button>

      {r && (
        <div className="mt-4 space-y-3">
          <p className="text-sm text-stone-500">
            <span className="font-semibold uppercase tracking-wide text-stone-800">{r.status}</span> · {r.trigger} ·{" "}
            {new Date(r.started_at).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })}
          </p>
          {r.steps.length > 0 && (
            <pre ref={log} className="max-h-40 overflow-y-auto rounded-lg bg-stone-900 p-3 font-mono text-xs leading-relaxed text-stone-200">
              {r.steps.join("\n")}
            </pre>
          )}
          {r.summary && <p className="text-base">{r.summary}</p>}
          {r.diff ? (
            <pre className="max-h-72 overflow-auto rounded-lg border border-stone-200 font-mono text-xs leading-relaxed">
              {r.diff.split("\n").map((l, i) => (
                <div key={i} className={`px-3 ${diffLine(l)}`}>{l || " "}</div>
              ))}
            </pre>
          ) : (
            r.status === "proposed" && <p className="text-sm text-stone-500">No change proposed.</p>
          )}
          {r.status === "proposed" && r.diff && (
            <div className="flex gap-3">
              <button
                disabled={busy}
                onClick={() => send("/review/approve")}
                className="rounded-lg bg-stone-900 px-4 py-2 text-base font-medium text-white hover:bg-stone-700 disabled:opacity-50"
              >
                Approve change
              </button>
              <button
                disabled={busy}
                onClick={() => send("/review/reject")}
                className="rounded-lg border border-stone-300 bg-white px-4 py-2 text-base font-medium hover:bg-stone-50 disabled:opacity-50"
              >
                Reject
              </button>
            </div>
          )}
        </div>
      )}
      {err && <pre className="mt-3 whitespace-pre-wrap rounded-lg bg-red-50 p-3 font-mono text-sm text-red-800">{err}</pre>}
    </Panel>
  );
}
