/**
 * Thin wrapper around Supabase's PostgREST API — same approach as the voice agent's own
 * booking_db.py (plain REST calls, no SDK). This file must only ever be imported from
 * Server Components, Server Actions, or Route Handlers — never from a "use client" file —
 * since it reads SUPABASE_SERVICE_KEY, which bypasses Row Level Security.
 */

function getConfig() {
  const url = process.env.SUPABASE_URL?.replace(/\/$/, "");
  const key = process.env.SUPABASE_SERVICE_KEY;
  if (!url || !key) {
    throw new Error("SUPABASE_URL or SUPABASE_SERVICE_KEY not set in .env.local");
  }
  return { url, key };
}

function headers(key: string, extra?: Record<string, string>) {
  return {
    apikey: key,
    Authorization: `Bearer ${key}`,
    "Content-Type": "application/json",
    ...extra,
  };
}

/** GET rows from a table with a raw PostgREST query string, e.g. "order=created_at.desc&limit=50". */
export async function fetchRows<T>(table: string, query: string): Promise<T[]> {
  const { url, key } = getConfig();
  const res = await fetch(`${url}/rest/v1/${table}?${query}`, {
    headers: headers(key),
    cache: "no-store",
  });
  if (!res.ok) {
    throw new Error(`Supabase fetch failed for ${table} (${res.status}): ${await res.text()}`);
  }
  return res.json();
}

/** PATCH a single row by id. Used for the one write this dashboard performs — updating a
 * lead's status as the sales team works it. */
export async function patchRow(
  table: string,
  id: number | string,
  body: Record<string, unknown>
): Promise<void> {
  const { url, key } = getConfig();
  const res = await fetch(`${url}/rest/v1/${table}?id=eq.${id}`, {
    method: "PATCH",
    headers: headers(key, { Prefer: "return=minimal" }),
    body: JSON.stringify(body),
    cache: "no-store",
  });
  if (!res.ok) {
    throw new Error(`Supabase patch failed for ${table}#${id} (${res.status}): ${await res.text()}`);
  }
}
