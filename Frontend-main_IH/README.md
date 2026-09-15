# Innoventix Console

A read-mostly admin dashboard for the Innoventix Hub voice agent — calls, bookings, and
leads, pulled live from the same Supabase project the voice agent (`booking_db.py`) writes
to. Built with Next.js 14 (App Router) and TypeScript.

## What it shows

- **Overview** — rolling 24-hour call volume, meetings booked, leads captured, dropped
  calls, and a recent-calls list.
- **Calls** — every call log, searchable by phone number, filterable by outcome, with
  expandable transcripts.
- **Bookings** — every meeting booked through the agent, with a link to join.
- **Leads** — callers who showed interest without booking. The one write this dashboard
  performs: updating a lead's status (warm / contacted / converted / cold) as your team
  works through them.

## Setup

1. **Install dependencies:**
   ```
   npm install
   ```

2. **Copy the env file and fill it in:**
   ```
   cp .env.local.example .env.local
   ```
   - `SUPABASE_URL` / `SUPABASE_SERVICE_KEY` — same values as the voice agent's own `.env`.
   - `ADMIN_PASSWORD` — a password for accessing this dashboard. There's no per-user login,
     just one shared password, since this is an internal tool. Don't reuse a personal
     password here.
   - `SESSION_SECRET` — a random string used to sign the login cookie. Generate one with:
     ```
     openssl rand -hex 32
     ```

3. **Run it:**
   ```
   npm run dev
   ```
   Visit `http://localhost:3000` — you'll be redirected to `/login` first.

## Security notes

- `SUPABASE_SERVICE_KEY` bypasses Row Level Security and is only ever read server-side
  (Server Components and Server Actions) — it's never sent to the browser. Don't rename any
  `.env.local` variable with a `NEXT_PUBLIC_` prefix, which would bundle it into client-side
  JS.
- This dashboard shows real customer data — names, phone numbers, emails, call transcripts.
  The password gate (`middleware.ts`) is intentionally simple (one shared password, no user
  accounts) — appropriate for a small internal team, not for a public-facing deployment.
  If you need per-user accounts or audit logging of who viewed what, that's a bigger change
  than this dashboard currently makes.
- The session cookie is `httpOnly` and signed (HMAC-SHA256 via `SESSION_SECRET`), so it
  can't be read or forged by client-side JS or by guessing.

## Deploying

Works on Vercel or any Node hosting that supports Next.js 14. Set the same four
`.env.local` variables as environment variables in your hosting provider — don't commit
`.env.local` to version control (it's already in `.gitignore`).

## Extending

- `lib/types.ts` mirrors `innoventix_schema.sql` from the voice agent project — if that
  schema changes, update this file to match.
- `lib/supabase.ts` is the only place that talks to Supabase — add new query functions
  there rather than calling `fetch` directly from a page.
- Each page fetches its own data server-side (`export const dynamic = "force-dynamic"`
  disables caching, since this is live operational data, not content that should be
  statically generated).
